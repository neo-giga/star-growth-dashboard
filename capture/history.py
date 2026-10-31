"""GitHub's per-day star history: GET /repos/{owner}/{name}/stargazers/history.

One page holds 30 calendar weeks, newest first; each week is {week: Unix time of its Sunday 00:00 UTC, total, days: [7 counts,
Sunday first]}. The counts are the stars still present today bucketed by the day they were given, and the day boundaries are
US Pacific (measured 2026-10-29: the bucket of the current day grew from 9 to 18 between 07:15 and 07:36 UTC, i.e. a day that
began at 07:00 UTC). Two pages (60 weeks = 420 days) cover a 365-day window.

A repo's folded history is {start, days, through, anchor_at, anchor_stars, pages, has_more}: `days` run from `start` to `through`
inclusive, where `through` is the Pacific date of the anchor and that day's bucket is partial (stars given before the anchor).
The anchor is the repo's star count read seconds before its history, so stars after `through` are the snapshot's business:
gain over a window that starts inside the history = history days from the window start to `through` + (today's count - anchor).
"""
from __future__ import annotations
import datetime as dt
import zoneinfo
from .github import GitHubError
from .snapshot import read_counts

PACIFIC = zoneinfo.ZoneInfo('America/Los_Angeles')
WINDOWS = (1, 7, 15, 30, 90, 180, 365)
WEEKS_PER_PAGE = 30
# A star given between a repo's anchor and its history read is counted twice (in the history and in today - anchor), so the
# read starts and ends at most this long after the anchor, or the repo is anchored again. Batches run ~25 s (measured 2026-10-29:
# 16 s median, 33 s max from anchor to page 1 on 1,878 repos).
MAX_ANCHOR_AGE = 120


def pacific_date(instant):
    """The Pacific calendar date of an aware datetime."""
    return instant.astimezone(PACIFIC).date()


def parse_instant(text):
    return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))


def read_pages(gh, full_name, pages=2, first_page=1):
    """[(page, weeks, has_next)] for pages first_page..first_page+pages-1, stopping at the provider's end."""
    out = []
    for page in range(first_page, first_page + pages):
        weeks, headers = gh.rest(f'/repos/{full_name}/stargazers/history', {'page': page, 'per_page': WEEKS_PER_PAGE})
        has_next = 'rel="next"' in (headers.get('link') or '')
        out.append((page, weeks, has_next))
        if not has_next:
            break
    return out


def anchored_reads(gh, batch, read, wall, requests_per_repo=1, max_age=MAX_ANCHOR_AGE, stats=None):
    """Read one batch (at most 100 node IDs) of histories next to their anchors. Yields (node_id, status, anchor_at, anchor, result):
    'ok' (result = read(full_name)), 'missing' (the anchor query answered null: deleted or private), 'failed' (the anchor query
    itself failed after its retries: unread, try again later) or 'error' (read raised GitHubError, the result).
    The batch's core budget is waited for before its anchors are read, so a throttle wait never sits between an anchor and its
    read; a repo whose read would start, or did end, more than max_age after its anchor is anchored again (and read again, once).
    stats (a dict) collects anchor_points and anchor_queries. When the client's deadline passes, GitHubError('deadline') is raised
    out of the batch (before the next repo, or from a read), never reported as one repo's failure."""
    stats = stats if stats is not None else {}
    gh.wait_core(len(batch) * requests_per_repo)
    state = {'at': None, 'rows': {}, 'failed': set()}

    def anchor(ids):
        failed = []
        state['at'] = wall()
        state['rows'], counted = read_counts(gh, ids, workers=1, failed=failed)
        state['failed'] = set(failed)
        stats['anchor_points'] = stats.get('anchor_points', 0) + counted['points']
        stats['anchor_queries'] = stats.get('anchor_queries', 0) + counted['queries']

    def stale():
        return state['at'] is None or (wall() - state['at']).total_seconds() > max_age

    def check_deadline():
        if gh.deadline is not None and gh.clock() >= gh.deadline:
            raise GitHubError('deadline', 'the deadline has passed')

    for k, node_id in enumerate(batch):
        for attempt in (1, 2):
            check_deadline()
            if stale():
                anchor(batch[k:])
                check_deadline()  # an anchor query cut by the deadline is not a failed anchor
            if node_id in state['failed']:
                yield node_id, 'failed', state['at'], None, None
                break
            row = state['rows'].get(node_id)
            if row is None:
                yield node_id, 'missing', state['at'], None, None
                break
            try:
                result = read(row['full_name'])
            except GitHubError as error:
                if error.status == 'deadline':
                    raise
                yield node_id, 'error', state['at'], row, error
                break
            if attempt == 1 and stale():
                continue  # the read outlasted the bound (retry waits): anchor again and read again
            yield node_id, 'ok', state['at'], row, result
            break


def fold(weeks, anchor_at, anchor_stars, pages, has_more):
    """Weeks (any order, duplicates by week key allowed; the later entry wins) -> the stored history record."""
    anchor_at = anchor_at if isinstance(anchor_at, dt.datetime) else parse_instant(anchor_at)
    through = pacific_date(anchor_at)
    by_week = {}
    for week in weeks:
        by_week[int(week['week'])] = [int(n) for n in week['days']]
    if not by_week:
        return {'start': None, 'days': [], 'through': through.isoformat(), 'anchor_at': anchor_at.isoformat(), 'anchor_stars': anchor_stars,
                'pages': pages, 'has_more': has_more}
    sundays = sorted(by_week)
    start = dt.datetime.fromtimestamp(sundays[0], dt.timezone.utc).date()
    days = []
    for sunday in sundays:
        date = dt.datetime.fromtimestamp(sunday, dt.timezone.utc).date()
        gap = (date - (start + dt.timedelta(days=len(days)))).days
        days.extend([0] * max(gap, 0))  # a missing week (never seen in practice) counts as no stars
        days.extend(by_week[sunday])
    keep = (through - start).days + 1
    days = days[:max(keep, 0)]
    if keep > len(days):  # history ends before the anchor day (weeks are only listed up to the current one, so this is a guard)
        days.extend([0] * (keep - len(days)))
    return {'start': start.isoformat(), 'days': days, 'through': through.isoformat(), 'anchor_at': anchor_at.isoformat(),
            'anchor_stars': anchor_stars, 'pages': pages, 'has_more': has_more}


def record_complete(record):
    """True when a folded record can serve every window: it holds days, and either no older history exists or it reaches back
    more than the longest window before its anchor day. An empty record, or one page of a repo with older weeks unread, is not
    complete: the repo stays queued (or in repair) and is read again in turn."""
    if not record or record.get('start') is None:
        return False
    if not record.get('has_more'):
        return True
    return dt.date.fromisoformat(record['start']) <= dt.date.fromisoformat(record['through']) - dt.timedelta(days=max(WINDOWS) + 1)


def stars_between(record, first, last):
    """Sum of the history days from `first` to `last` inclusive (dates), clipped to the record; None when the record cannot
    serve the range (it starts after `first` with older history still unread, or it ends before `last`)."""
    if not record or record.get('start') is None:
        return None
    start = dt.date.fromisoformat(record['start'])
    through = dt.date.fromisoformat(record['through'])
    if last > through:
        return None
    if first < start and record.get('has_more'):
        return None
    lo = max((first - start).days, 0)
    hi = (last - start).days + 1
    return sum(record['days'][lo:hi]) if hi > lo else 0
