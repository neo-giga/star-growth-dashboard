"""Capture today's star count of every known repo, find new repos above the threshold, and write the page-ready windows file.

Order matters: the snapshot of the known set is written first, discovery (search, best effort, deadline) comes second and never
blocks it, repos without history (oldest turn first, at most MAX_HISTORY_PER_DAY) get their two history pages, then
repos.json, the history queue, the windows file and the diagnostic. The threshold is exclusive: --threshold 999 captures every
repo with at least 1,000 stars. A same-day rerun keeps the earlier run's counts for the repos it could not read.
The day's counts live in the snapshot only: repos.json carries no per-day field, so it only changes when a repo enters, is renamed or changes language (measured 2026-10-29: a daily `stars` + `last_seen` cost
4.6 MiB of pack in 14 days, ~120 MiB a year, for data the snapshot already holds). Exit codes:
0 a complete capture; 3 the files are written but the capture is partial (discovery not complete, no search total today, a
count batch unread after its retry, or coverage under COVERAGE_FLOOR), so the run should go red after committing and the
publisher keeps the previous page; 2 nothing usable was written.
"""
from __future__ import annotations
import argparse, datetime as dt, itertools, json, pathlib, shutil, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from capture.discovery import WORKERS as DISCOVERY_WORKERS, discover, total_count
from capture.github import GitHub, GitHubError, token_from_environment
from capture.history import anchored_reads, fold, pacific_date, read_pages, record_complete
from capture.snapshot import read_counts
from capture.store import DataDir
from capture.windows import build_page

COVERAGE_FLOOR = 0.99
# Repos without history read per day (2 core requests each). GITHUB_TOKEN allows 1,000 core requests an hour; the one-time backfill
# (scripts/backfill_history.py) reads the bulk, this covers the day's entrants and any leftovers.
MAX_HISTORY_PER_DAY = 400
CORE_PER_HOUR = 900  # the client's sliding-hour core budget: under GITHUB_TOKEN's 1,000, retries included
HISTORY_DEADLINE_MINUTES = 110  # no history request starts after this (from the run's start): 40 of the job's 150 min stay for writing and committing


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def read_counts_twice(gh, ids, workers):
    """read_counts, then once more one batch at a time for the ids of the batches that failed: (rows, stats, ids still unread)."""
    failed = []
    rows, stats = read_counts(gh, ids, workers=workers, failed=failed)
    if failed:
        unread = []
        again, again_stats = read_counts(gh, failed, workers=1, failed=unread)
        rows.update(again)
        stats['retry'] = again_stats
        stats['points'] += again_stats['points']
        failed = unread
    stats['unread'] = len(failed)
    return rows, stats, failed


def history_turn(entry, node_id):
    """Queue order: the date the repo last had a turn (its entry date until its first attempt), then fewer attempts, then entry
    date, then ID. A repo whose read failed goes behind every repo that has waited longer (and behind one never tried on the same
    day), so neither new arrivals nor failing IDs starve the rest."""
    return (entry.get('last_attempt') or entry['since'], entry.get('attempts', 0), entry['since'], node_id)


def completeness(threshold, counts, discovery, total, total_source, known_unread, entrants_unread, inaccessible):
    """How complete today's capture is, for the page, the feed and the publish gate: complete only when discovery listed every
    slice, the denominator is today's search total, no count batch stayed unread and coverage is at the floor. A search total
    counts what GitHub's index lists at that minute; it is not a census of GitHub."""
    rows = sum(1 for stars in counts.values() if stars > threshold)
    ratio = round(rows / total, 4) if total else None
    reasons = []
    if discovery != 'complete':
        reasons.append(f'discovery {discovery}')
    if total_source != 'search':
        reasons.append('no search total today' if total is None else 'search total from an earlier capture')
    if known_unread:
        reasons.append(f'{known_unread} known repositories unread')
    if entrants_unread:
        reasons.append(f'{entrants_unread} new repositories unread')
    if ratio is not None and ratio < COVERAGE_FLOOR:
        reasons.append(f'coverage {ratio:.2%} under {COVERAGE_FLOOR:.0%}')
    return {'status': 'partial' if reasons else 'complete', 'reasons': reasons, 'rows': rows, 'search_total': total, 'total_count_source': total_source,
            'coverage': ratio, 'discovery': discovery, 'known_unread': known_unread, 'entrants_unread': entrants_unread, 'inaccessible': inaccessible}


def run(gh, data, threshold, now=None, workers=4, deadline_minutes=60, skip_discovery=False, sample=None, max_slices=None, max_history=MAX_HISTORY_PER_DAY,
        discovery_workers=DISCOVERY_WORKERS, wall=utcnow, log=print, history_deadline_minutes=HISTORY_DEADLINE_MINUTES):
    now = now or wall()
    date = pacific_date(now)
    started = time.monotonic()
    diagnostic = {'schema': 'star-growth-dashboard/capture-diagnostic/1', 'date': date.isoformat(), 'captured_at': now.isoformat(), 'threshold': threshold,
                  'snapshot': None, 'discovery': {'status': 'skipped'}, 'new_entrants': {'count': 0, 'history_read': 0, 'history_failed': 0},
                  'coverage': None, 'windows': None, 'exit_code': 2, 'seconds': 0}
    repos_doc = data.read_repos()
    known = repos_doc.get('repos') or {}
    meta = data.read_meta()
    ids = sorted(known)
    if sample is not None:
        ids = ids[:sample]
    earlier = dict(data.read_snapshot(date) or {})  # a same-day rerun: the earlier run's counts stand for the repos this one cannot read
    # 1. the snapshot of the known set: the one thing a day must not lose
    rows, stats, unread = read_counts_twice(gh, ids, workers) if ids else ({}, {'asked': 0, 'read': 0, 'missing': 0, 'queries': 0, 'points': 0, 'failed_queries': 0, 'seconds': 0, 'unread': 0}, [])
    diagnostic['snapshot'] = stats
    counts = {}
    for node_id, row in rows.items():
        counts[node_id] = row['stars']
        known[node_id].update({'full_name': row['full_name'], 'language': row['language'], 'created_at': row['created_at']})
    inaccessible = sorted(set(ids) - set(rows) - set(unread))  # the answer said null: deleted, private or not a repository
    carried = {i: earlier[i] for i in known if i not in counts and i not in inaccessible and i in earlier}
    counts.update(carried)
    known_unread = sorted(i for i in known if i not in counts and i not in inaccessible)
    stats['carried_from_earlier_run'] = len(carried)
    if counts:
        data.write_snapshot(date, now.isoformat(), counts, threshold)
    log(f'snapshot: {stats["read"]}/{stats["asked"]} known repos read, {stats["points"]} points, {stats["failed_queries"]} failed queries, {stats["seconds"]} s')
    # 2. discovery: best effort, never blocks
    total, total_source = None, 'none'
    entrants = {}
    if not skip_discovery:
        try:
            total = total_count(gh, threshold)
            total_source = 'search' if total is not None else 'none'
            elapsed = time.monotonic() - started
            result = discover(gh, threshold, slices=data.read_slices(), deadline_seconds=max(deadline_minutes * 60 - elapsed, 60), max_slices=max_slices, workers=discovery_workers,
                              progress=lambda s, n: log(f'discovery: stars {s["lo"]}..{s["hi"] or ""} total={s["total"]} got={s["got"]} {s["status"]} '
                                                        f'(pages {s["pages"]} x {s["page_size"]}); {n} repos so far'))
            entrants = {i: r for i, r in result['repos'].items() if i not in known}
            diagnostic['discovery'] = {k: result[k] for k in ('status', 'slices_ok', 'slices_failed', 'slices_skipped', 'seconds', 'page_size')}
            diagnostic['discovery'].update({'repos_listed': len(result['repos']), 'new_entrants': len(entrants), 'slices': result['slices']})
            data.write_slices(result['next_plan'])
        except Exception as error:  # noqa: BLE001 - discovery must never take the snapshot down with it
            diagnostic['discovery'] = {'status': 'failed', 'error': repr(error)[:300]}
        log(f'discovery: {diagnostic["discovery"].get("status")}, total_count={total}, new entrants={len(entrants)}')
    # an earlier total only stands in for the same threshold (a total for >2000 is no denominator for >999)
    if total is None and meta.get('last_total_count') and meta.get('last_total_count_threshold', repos_doc.get('threshold')) == threshold:
        total, total_source = meta['last_total_count'], 'previous'
    # 3. new entrants join the known set and today's snapshot
    entrants_unread, entrants_inaccessible = [], []
    if entrants:
        fresh, fresh_stats, entrants_unread = read_counts_twice(gh, sorted(entrants), workers)
        entrants_inaccessible = sorted(set(entrants) - set(fresh) - set(entrants_unread))
        diagnostic['snapshot']['entrant_queries'] = fresh_stats['queries']
        diagnostic['snapshot']['points'] += fresh_stats['points']
        diagnostic['new_entrants']['snapshot'] = fresh_stats
        for node_id, item in entrants.items():
            known[node_id] = {'full_name': item['full_name'], 'language': item.get('language'), 'created_at': item.get('created_at'), 'first_seen': date.isoformat()}
            if node_id in fresh:
                counts[node_id] = fresh[node_id]['stars']
                known[node_id].update({'full_name': fresh[node_id]['full_name'], 'language': fresh[node_id]['language'], 'created_at': fresh[node_id]['created_at']})
        diagnostic['new_entrants']['count'] = len(entrants)
        if counts:
            data.write_snapshot(date, now.isoformat(), counts, threshold)
    # 4. history for eligible repos without a complete record (capture.history.record_complete), at most max_history a day, inside
    # the core budget and the history deadline. Today's entrants join the durable pending queue (history-pending.json); its rows
    # are shown as pending, not hidden and not zero. A known repo whose record is missing or incomplete goes to the repair queue
    # instead: read in turn like the pending ones but never pending, and given at most half the day's reads while entrants wait.
    history = data.read_history_all()
    queue, repair = data.read_pending(), data.read_repair()
    for node_id in entrants:
        if not record_complete(history.get(node_id)):
            queue.setdefault(node_id, {'since': date.isoformat()})
    for waiting_list in (queue, repair):
        for node_id in [i for i in waiting_list if record_complete(history.get(i))]:  # completed by another path (a backfill)
            del waiting_list[node_id]
    lacking = [i for i, stars in counts.items() if stars > threshold and not record_complete(history.get(i))]
    for node_id in lacking:
        if node_id not in queue:
            repair.setdefault(node_id, {'since': date.isoformat()})
    waiting = sorted((i for i in lacking if i in queue), key=lambda i: history_turn(queue[i], i))
    fixing = sorted((i for i in lacking if i in repair), key=lambda i: history_turn(repair[i], i))
    fixing = fixing[:max(max_history // 2, max_history - len(waiting))]
    waiting = waiting[:max_history - len(fixing)]
    picked = [i for pair in itertools.zip_longest(fixing, waiting) for i in pair if i is not None]  # a deadline cuts both lanes alike
    diagnostic['new_entrants']['history_pending'] = len(lacking)
    report = {'queue': len(queue), 'repair_queue': len(repair), 'eligible_without_history': len(lacking), 'repair': len(fixing), 'selected': len(picked),
              'read': 0, 'incomplete': 0, 'failed': 0, 'unread': 0, 'deferred': 0, 'core_per_hour': gh.core_per_hour}
    if picked:
        anchor_stats, records = {}, {}
        stop_at = gh.clock() + max(history_deadline_minutes * 60 - (time.monotonic() - started), 0)
        gh.deadline = stop_at  # every request, retry and budget wait stops there: the job's timeout must never cut the commit
        done = 0
        try:
            for start in range(0, len(picked), 100):  # the anchor is the count seconds before the history read, per batch of 100
                for node_id, status, anchor_at, anchor, result in anchored_reads(gh, picked[start:start + 100], lambda name: read_pages(gh, name, pages=2), wall,
                                                                                 requests_per_repo=2, stats=anchor_stats):
                    done += 1
                    entry = queue.get(node_id) or repair.get(node_id)
                    if status == 'ok':
                        weeks = [w for _, page_weeks, _ in result for w in page_weeks]
                        record = fold(weeks, anchor_at, anchor['stars'], pages=len(result), has_more=result[-1][2])
                        old = history.get(node_id)
                        if record_complete(record) or old is None or (record['start'] and (old.get('start') is None or record['start'] <= old['start'])):
                            records[node_id] = record  # keep the partial work unless an earlier record reaches further back
                        if record_complete(record):
                            queue.pop(node_id, None); repair.pop(node_id, None)
                            diagnostic['new_entrants']['history_read'] += 1
                            report['read'] += 1
                            continue
                        report['incomplete'] += 1
                        status = 'incomplete'
                    elif status == 'error':
                        diagnostic['new_entrants']['history_failed'] += 1
                        report['failed'] += 1
                    else:
                        report['unread'] += 1
                    entry.update(attempts=entry.get('attempts', 0) + 1, last_attempt=date.isoformat(),
                                 last_status=status if status != 'error' else f'error {getattr(result, "status", "")}'.strip())
        except GitHubError as error:  # the history deadline (before a request, a repo or a budget wait): the rest waits for tomorrow
            report['stopped'] = repr(error)[:200]
        finally:
            gh.deadline = None
        report['deferred'] = len(picked) - done
        diagnostic['snapshot']['points'] += anchor_stats.get('anchor_points', 0)
        if records:
            data.write_history(records)
            history.update(records)
    retrying = {name: {i: e for i, e in lane.items() if e.get('attempts')} for name, lane in (('pending', queue), ('repair', repair))}
    report.update(pending_after=sum(1 for i in counts if counts[i] > threshold and i in queue), repair_after=sum(1 for i in counts if counts[i] > threshold and i in repair),
                  pending_retrying=len(retrying['pending']), repair_retrying=len(retrying['repair']),
                  retrying_sample={name: dict(sorted(lane.items(), key=lambda kv: -kv[1]['attempts'])[:20]) for name, lane in retrying.items()})
    diagnostic['history'] = report
    data.write_pending(queue, now.isoformat(), repair)
    # 5. the known set, the meta, the windows, the diagnostic
    repos_doc.update({'threshold': threshold, 'updated_at': now.isoformat(), 'repos': dict(sorted(known.items()))})
    data.write_repos(repos_doc)
    first = meta.get('first_snapshot_date')
    first_snapshot = min(dt.date.fromisoformat(first), date) if first else (date if counts else None)
    if total is not None and total_source == 'search':
        meta.update({'last_total_count': total, 'last_total_count_at': now.isoformat(), 'last_total_count_source': total_source, 'last_total_count_threshold': threshold})
    meta.update({'first_snapshot_date': first_snapshot.isoformat() if first_snapshot else None, 'last_capture_date': date.isoformat() if counts else meta.get('last_capture_date'),
                 'last_capture_at': now.isoformat() if counts else meta.get('last_capture_at')})
    data.write_meta(meta)
    status = completeness(threshold, counts, diagnostic['discovery'].get('status'), total, total_source, len(known_unread), len(entrants_unread),
                          len(inaccessible) + len(entrants_inaccessible))
    diagnostic['completeness'] = status
    diagnostic['counts'] = {'known_asked': len(ids), 'known_read': len(rows), 'carried_from_earlier_run': len(carried), 'known_unread': len(known_unread),
                            'known_unread_ids': known_unread[:100], 'inaccessible': len(inaccessible), 'entrants_listed': len(entrants),
                            'entrants_unread': len(entrants_unread), 'entrants_unread_ids': entrants_unread[:100], 'entrants_inaccessible': len(entrants_inaccessible)}
    page = None
    if counts:
        stamp = now.isoformat()  # one generation: the snapshot, repos.json, meta.json, the queue and the diagnostic all carry it
        page = build_page(date, stamp, threshold, known, counts, data.snapshot_dates(), data.read_snapshot, history, first_snapshot, pending=queue, capture=status,
                          repair=repair, generation={'snapshot': stamp, 'repos': stamp, 'meta': stamp, 'queue': stamp, 'diagnostic': stamp})
        data.write_windows(page)
        diagnostic['windows'] = {'rows': page['row_count'], 'history_records': len(history), 'per_window': page['windows']}
    above = sum(1 for stars in counts.values() if stars > threshold)
    ratio = (above / total) if total else None
    diagnostic['coverage'] = {'rows_above_threshold': above, 'total_count': total, 'total_count_source': total_source, 'ratio': round(ratio, 4) if ratio is not None else None,
                              'floor': COVERAGE_FLOOR, 'ok': ratio is not None and ratio >= COVERAGE_FLOOR}
    # Green only for a complete capture: every discovery slice listed, today's search total, nothing unread, coverage at the floor.
    # A partial capture is still written and committed (a lost day is worse than a red run); the publisher keeps the previous page.
    diagnostic['exit_code'] = 2 if not counts else (0 if status['status'] == 'complete' else 3)
    diagnostic['seconds'] = round(time.monotonic() - started, 1)
    data.write_diagnostic(date, diagnostic)
    log(f'coverage: {above} rows above {threshold} vs total_count {total} ({total_source}) -> {diagnostic["coverage"]["ok"]}; exit {diagnostic["exit_code"]}')
    return diagnostic['exit_code'], diagnostic


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--data-dir', help='checkout of the data branch (read and written); with --dry-run only its repos/slices/meta are copied')
    parser.add_argument('--threshold', type=int, default=2000, help='repos with more stars than this (default 2000; 999 = at least 1,000 stars)')
    parser.add_argument('--workers', type=int, default=4, help='concurrent GraphQL queries, at most 4')
    parser.add_argument('--discovery-workers', type=int, default=DISCOVERY_WORKERS, help=f'search pages in flight at once, 1..8 (default {DISCOVERY_WORKERS}; the 30/min pacing always holds)')
    parser.add_argument('--deadline-minutes', type=float, default=60, help='discovery stops (partial) this long after the run started')
    parser.add_argument('--skip-discovery', action='store_true', help='snapshot the known set only')
    parser.add_argument('--sample', type=int, help='snapshot at most this many known repos (dry runs)')
    parser.add_argument('--max-slices', type=int, help='list at most this many discovery slices (dry runs; the rest are skipped)')
    parser.add_argument('--max-history', type=int, default=MAX_HISTORY_PER_DAY, help=f'read history for at most this many repos that lack it (default {MAX_HISTORY_PER_DAY})')
    parser.add_argument('--core-per-hour', type=int, default=CORE_PER_HOUR, help=f'sliding-hour core request budget (default {CORE_PER_HOUR})')
    parser.add_argument('--history-deadline-minutes', type=float, default=HISTORY_DEADLINE_MINUTES, help=f'no history request starts this long after the run started (default {HISTORY_DEADLINE_MINUTES})')
    parser.add_argument('--diagnostic', help='also write the diagnostic JSON here')
    parser.add_argument('--request-log', help='append one JSON line per HTTP request here (timestamp, URL, status, rate-limit remaining)')
    parser.add_argument('--dry-run', action='store_true', help='write into a fresh temporary directory, printed at the end, instead of --data-dir')
    parser.add_argument('--now', help='ISO instant to stamp the capture with (tests); default: now')
    args = parser.parse_args(argv)
    if not args.data_dir and not args.dry_run:
        parser.error('--data-dir or --dry-run is required')
    if not 1 <= args.workers <= 4:
        parser.error('--workers must be 1..4')
    if not 1 <= args.discovery_workers <= 8:
        parser.error('--discovery-workers must be 1..8')
    root = pathlib.Path(args.data_dir) if args.data_dir else None
    if args.dry_run:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='capture-dry-run-'))
        if root:
            for name in ('repos.json', 'slices.json', 'meta.json', 'history-pending.json'):
                if (root / name).exists():
                    shutil.copy(root / name, tmp / name)
        root = tmp
    now = dt.datetime.fromisoformat(args.now.replace('Z', '+00:00')) if args.now else None
    def log_request(entry):
        with open(args.request_log, 'a') as handle:
            handle.write(json.dumps(entry, separators=(',', ':')) + '\n')

    gh = GitHub(token_from_environment(), request_log=log_request if args.request_log else None, core_per_hour=args.core_per_hour)
    code, diagnostic = run(gh, DataDir(root), args.threshold, now=now, workers=args.workers, deadline_minutes=args.deadline_minutes,
                           skip_discovery=args.skip_discovery, sample=args.sample, max_slices=args.max_slices, max_history=args.max_history,
                           discovery_workers=args.discovery_workers, history_deadline_minutes=args.history_deadline_minutes)
    diagnostic['data_dir'] = str(root)
    diagnostic['dry_run'] = args.dry_run
    diagnostic['requests'] = dict(gh.counts)
    if args.diagnostic:
        pathlib.Path(args.diagnostic).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.diagnostic).write_text(json.dumps(diagnostic, indent=1))
    print(json.dumps({k: v for k, v in diagnostic.items() if k not in ('discovery', 'windows')} | {'discovery_status': diagnostic['discovery'].get('status')}, indent=1))
    if args.dry_run:
        print(f'dry run wrote {root}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
