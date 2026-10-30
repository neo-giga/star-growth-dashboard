"""Find every public repo above the star threshold with REST search, in star-range slices of at most 800 results.

Search caps a query at 1,000 results (page 11 of stars:>2000 answers 422), so the set is listed slice by slice:
stars:lo..hi fork:true, 100 per page, sorted by stars ascending. Forks are in the set: search leaves them out unless the query
says fork:true (measured 2026-10-29: stars:>2000 counts 33,891 without it and 34,193 with it; 302 forks). Page 1 of a slice
carries total_count; a slice over the cap is halved and both halves are listed. Yesterday's slice boundaries are the starting
plan, so a healthy run lists without probing.
When search is degraded it answers 502 after about 10 s for 100-result pages while 50- and 30-result pages still succeed
(measured 2026-10-29 17:40 UTC), so a slice's page 1 probes the page size with two attempts and a failure drops the size
to the next smaller one for the rest of the run; later pages retry in full at the slice's size (re-listing a slice on a
later page's failure threw away its fetched pages: 148 wasted requests on 2026-10-29). A slice's pages are read by a few
workers, and the next slices open while they are in flight, so 10 s answers do not starve the 30-a-minute budget.
Discovery is best effort: a slice whose pages keep failing is recorded as failed or partial, a deadline leaves later
slices skipped, and the result says so. The repos found are kept either way. A page answered with incomplete_results (the
search timed out on GitHub's side) keeps its items but makes its slice partial; such a total_count is not trusted.
"""
from __future__ import annotations
import collections, concurrent.futures, math, time
from .github import GitHubError, TRANSIENT

MAX_PER_SLICE = 800
TARGET_PER_SLICE = 600
PAGE_SIZES = (100, 50, 30)
QUICK_ATTEMPTS = 2  # attempts at a page size before the next smaller one is tried
WORKERS = 8  # pages of one slice in flight at once; the 30-a-minute pacing holds whatever the count; 10 s answers plus retry waits need 8 to fill it
OPEN_FROM = 20000  # above this an open-ended stars:>=lo slice is tried first (527 repos had > 50k stars on 2026-10-29)


class Deadline(Exception):
    pass


def query(lo, hi):
    return (f'stars:>={lo}' if hi is None else f'stars:{lo}..{hi}') + ' fork:true'


def initial_plan(threshold, width=30):
    return [{'lo': threshold + 1, 'hi': threshold + width}]


def next_plan(slices, threshold, target=TARGET_PER_SLICE):
    """Tomorrow's boundaries from today's outcome: listed slices keep their ranges, adjacent small ones merge, and the plan is
    made contiguous from threshold+1 to an open-ended last slice."""
    ranges = sorted(((s['lo'], s['hi'], s.get('total')) for s in slices if s['status'] != 'split'), key=lambda r: r[0])
    merged = []
    for lo, hi, total in ranges:
        if merged and merged[-1][1] is not None and merged[-1][2] is not None and total is not None and merged[-1][2] + total <= target:
            merged[-1] = (merged[-1][0], hi, merged[-1][2] + total)
        else:
            merged.append((lo, hi, total))
    plan, cursor = [], threshold + 1
    for lo, hi, _ in merged:
        if hi is not None and hi < cursor:
            continue
        lo = max(lo, cursor)
        plan.append({'lo': lo, 'hi': hi})
        if hi is None:
            return plan
        cursor = hi + 1
    plan.append({'lo': cursor, 'hi': None})
    return plan


def total_count(gh, threshold):
    """search total_count for stars:>threshold fork:true, or None when search cannot answer or says its answer is incomplete."""
    try:
        answer = gh.search_repositories(f'stars:>{threshold} fork:true', page=1, per_page=1)
    except GitHubError:
        return None
    return None if answer.get('incomplete_results') else answer['total_count']


def transient(error):
    return error.status is None or error.status in TRANSIENT


def halves(lo, hi):
    """The two halves of a slice over the cap, or None when it is one star wide."""
    if hi is None:
        mid = lo * 2
    elif hi > lo:
        mid = lo + (hi - lo) // 2
    else:
        return None
    return {'lo': lo, 'hi': mid}, {'lo': mid + 1, 'hi': hi}


class Slice:
    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi
        self.size = None
        self.total = None
        self.pages = {}  # page -> items
        self.errors = []
        self.inflight = 0
        self.relist = False  # a transient failure at a size that has a smaller one: start again at the new size
        self.over = False

    def record(self, status):
        got = sum(len(v) for v in self.pages.values())
        rec = {'lo': self.lo, 'hi': self.hi, 'total': self.total, 'got': got, 'pages': len(self.pages), 'page_size': self.size, 'status': status}
        if self.errors:
            rec['error'] = str(self.errors[0])[:200]
        return rec


def discover(gh, threshold, slices=None, deadline_seconds=None, clock=time.monotonic, max_per_slice=MAX_PER_SLICE, target=TARGET_PER_SLICE,
             max_slices=None, progress=None, workers=WORKERS):
    """Slices are listed through one pool: a slice's later pages fan out as soon as its page 1 says how many there are, and the
    next slice's page 1 is fetched while they are in flight (several at once when the plan is yesterday's, one at a time when
    the plan is being made, since each slice's width comes from the previous total)."""
    started = clock()
    queue = collections.deque(slices or initial_plan(threshold))
    queue[0]['lo'] = threshold + 1  # the plan always starts at the threshold, whatever yesterday's was
    done, repos, listed = [], {}, 0
    planned_from_scratch = not slices
    state = {'page_size': PAGE_SIZES[0], 'proven': False}  # proven: a page 1 succeeded at the current size, so several slices may open at once
    gh.deadline = None if deadline_seconds is None else clock() + deadline_seconds  # retries and pacing waits stop at the deadline too
    futures = {}  # future -> (slice, page, size)
    opening = set()  # slices whose page 1 is in flight (bounded)
    opened = set()  # (lo, hi) of every slice opened, for max_slices
    stop = {'now': False}

    def time_left():
        return deadline_seconds is None or clock() - started < deadline_seconds

    def submit(pool, s, page):
        size = s.size if page > 1 else state['page_size']
        if page == 1:
            s.size = size
            opening.add(s)
        probing = page == 1 and size != PAGE_SIZES[-1]  # page 1 probes the size with few attempts; later pages retry in full at the slice's size
        future = pool.submit(gh.search_repositories, query(s.lo, s.hi), page, size, 'stars', 'asc', QUICK_ATTEMPTS if probing else None)
        futures[future] = (s, page, size)
        s.inflight += 1

    def finish(s):
        """Every page of s answered: record it, keep its repos, extend a from-scratch plan."""
        nonlocal listed
        if s.relist:
            opened.discard((s.lo, s.hi))  # the same slice again at the smaller size: not a new slice for max_slices
            queue.appendleft({'lo': s.lo, 'hi': s.hi})
            return
        if s.over:
            parts = halves(s.lo, s.hi)
            if parts:
                done.append(s.record('split'))
                queue.appendleft(parts[1])
                queue.appendleft(parts[0])
                return
        status = 'failed' if 1 not in s.pages else ('partial' if s.errors else 'ok')
        if status == 'failed':
            done.append(s.record(status))
            if s.hi is None:
                stop['now'] = True
            elif planned_from_scratch and not queue and not opening:
                queue.append({'lo': s.hi + 1, 'hi': None if s.hi + 1 >= OPEN_FROM else s.hi + (s.hi - s.lo + 1)})
            return
        for item in (item for page in sorted(s.pages) for item in s.pages[page]):
            repos[item['node_id']] = {'full_name': item['full_name'], 'stars': item['stargazers_count'], 'language': item.get('language'),
                                      'created_at': item.get('created_at')}
        done.append(s.record(status))
        listed += 1
        if progress:
            progress(done[-1], len(repos))
        if s.hi is None:
            stop['now'] = True

    def on_page_1(pool, s, answer):
        s.total = answer['total_count']
        s.pages[1] = answer['items']
        if s.total > max_per_slice and halves(s.lo, s.hi):
            s.over = True
            return
        for page in range(2, math.ceil(min(s.total, 1000) / s.size) + 1):
            submit(pool, s, page)
        if planned_from_scratch and s.hi is not None and not queue:
            width = max(1, int((s.hi - s.lo + 1) * target / max(s.total, 1)))
            # Above OPEN_FROM the planner tries one open-ended slice; if it is over the cap it is halved like any other (the halves
            # keep their bounds: on 2026-10-29 converting them back to open-ended split stars:>=22854 150 times until the deadline).
            queue.append({'lo': s.hi + 1, 'hi': None if s.hi + 1 >= OPEN_FROM else s.hi + width})

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            while queue or futures:
                may_open = 1 if planned_from_scratch or not state['proven'] else workers
                while queue and not stop['now'] and time_left() and len(opening) < may_open and (max_slices is None or len(opened) < max_slices):
                    nxt = queue.popleft()
                    lo, hi = nxt['lo'], nxt['hi']
                    opened.add((lo, hi))
                    submit(pool, Slice(lo, hi), 1)
                if not futures:
                    break
                ready, _ = concurrent.futures.wait(list(futures), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in ready:
                    s, page, size = futures.pop(future)
                    s.inflight -= 1
                    if page == 1:
                        opening.discard(s)
                    try:
                        answer = future.result()
                    except GitHubError as error:
                        if page == 1 and transient(error) and size != PAGE_SIZES[-1]:
                            state['page_size'] = PAGE_SIZES[PAGE_SIZES.index(size) + 1]
                            state['proven'] = False
                            s.relist = True
                        else:
                            s.errors.append(error)
                    except Exception as error:  # noqa: BLE001 - best effort: a surprise in one page must not lose the repos already found
                        s.errors.append(GitHubError('exception', repr(error)[:200]))
                    else:
                        if answer.get('incomplete_results'):
                            s.errors.append(GitHubError('incomplete', 'incomplete_results: true', query(s.lo, s.hi)))
                        if page == 1 and not s.relist:
                            state['proven'] = True
                            on_page_1(pool, s, answer)
                        else:
                            s.pages[page] = answer['items']
                    if s.inflight == 0:
                        finish(s)
    finally:
        gh.deadline = None
    for s in queue:  # a deadline, a slice cap or a failed open-ended slice leaves the rest of the plan unlisted today
        done.append({**s, 'status': 'skipped'})
    done.sort(key=lambda r: (r['lo'], r['hi'] is None, r['hi'] or 0))
    ok = sum(1 for s in done if s['status'] == 'ok')
    bad = sum(1 for s in done if s['status'] in ('failed', 'partial', 'skipped'))
    status = 'complete' if ok and not bad else ('partial' if ok or repos else 'failed')
    return {'status': status, 'repos': repos, 'slices': done, 'slices_ok': ok, 'slices_failed': sum(1 for s in done if s['status'] in ('failed', 'partial')),
            'slices_skipped': sum(1 for s in done if s['status'] == 'skipped'), 'seconds': round(clock() - started, 1), 'page_size': state['page_size'],
            'next_plan': next_plan(done, threshold, target)}
