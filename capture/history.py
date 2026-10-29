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

PACIFIC = zoneinfo.ZoneInfo('America/Los_Angeles')
WINDOWS = (1, 7, 15, 30, 90, 180, 365)
WEEKS_PER_PAGE = 30


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
