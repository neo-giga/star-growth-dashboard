"""Capture today's star count of every known repo, find new repos above the threshold, and write the page-ready windows file.

Order matters: the snapshot of the known set is written first, discovery (search, best effort, deadline) comes second and never
blocks it, repos without history (today's entrants first, at most MAX_HISTORY_PER_DAY) get their two history pages, then
repos.json, the windows file and the diagnostic. Exit codes:
0 fine; 3 the files are written but coverage (repos read with stars above the threshold / search total_count) is under
COVERAGE_FLOOR, so the run should go red after committing; 2 nothing usable was written.
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, shutil, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from capture.discovery import WORKERS as DISCOVERY_WORKERS, discover, total_count
from capture.github import GitHub, GitHubError, token_from_environment
from capture.history import fold, pacific_date, read_pages
from capture.snapshot import read_counts
from capture.store import DataDir
from capture.windows import build_page

COVERAGE_FLOOR = 0.99
# Repos without history read per day (2 core requests each). GITHUB_TOKEN allows 1,000 core requests an hour; the one-time backfill
# (scripts/backfill_history.py) reads the bulk, this covers the day's entrants and any leftovers.
MAX_HISTORY_PER_DAY = 400


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def run(gh, data, threshold, now=None, workers=4, deadline_minutes=60, skip_discovery=False, sample=None, max_slices=None, max_history=MAX_HISTORY_PER_DAY,
        discovery_workers=DISCOVERY_WORKERS, wall=utcnow, log=print):
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
    # 1. the snapshot of the known set: the one thing a day must not lose
    rows, stats = read_counts(gh, ids, workers=workers) if ids else ({}, {'asked': 0, 'read': 0, 'missing': 0, 'queries': 0, 'points': 0, 'failed_queries': 0, 'seconds': 0})
    diagnostic['snapshot'] = stats
    counts = {}
    for node_id, row in rows.items():
        counts[node_id] = row['stars']
        known[node_id].update({'full_name': row['full_name'], 'language': row['language'], 'created_at': row['created_at'], 'stars': row['stars'],
                               'last_seen': date.isoformat()})
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
    if total is None and meta.get('last_total_count'):
        total, total_source = meta['last_total_count'], 'previous'
    # 3. new entrants join the known set and today's snapshot
    if entrants:
        fresh, fresh_stats = read_counts(gh, sorted(entrants), workers=workers)
        diagnostic['snapshot']['entrant_queries'] = fresh_stats['queries']
        diagnostic['snapshot']['points'] += fresh_stats['points']
        diagnostic['new_entrants']['snapshot'] = fresh_stats
        for node_id, item in entrants.items():
            known[node_id] = {'full_name': item['full_name'], 'language': item.get('language'), 'created_at': item.get('created_at'), 'stars': item['stars'],
                              'first_seen': date.isoformat(), 'last_seen': date.isoformat()}
            if node_id in fresh:
                counts[node_id] = fresh[node_id]['stars']
                known[node_id].update({'full_name': fresh[node_id]['full_name'], 'language': fresh[node_id]['language'], 'created_at': fresh[node_id]['created_at'],
                                       'stars': fresh[node_id]['stars']})
        diagnostic['new_entrants']['count'] = len(entrants)
        if counts:
            data.write_snapshot(date, now.isoformat(), counts, threshold)
    # 4. history for repos that have none (today's entrants first), at most max_history a day: the backfill covers the bulk
    history = data.read_history_all()
    pending = sorted((i for i in counts if i not in history), key=lambda i: (known[i].get('first_seen') or '', i), reverse=True)[:max_history]
    diagnostic['new_entrants']['history_pending'] = sum(1 for i in counts if i not in history)
    if pending:
        anchor_at = wall()
        anchors, anchor_stats = read_counts(gh, pending, workers=workers)  # the anchor is the count seconds before the history read
        diagnostic['snapshot']['points'] += anchor_stats['points']
        records = {}
        for node_id in pending:
            if node_id not in anchors:
                continue
            try:
                pages = read_pages(gh, anchors[node_id]['full_name'], pages=2)
                weeks = [w for _, page_weeks, _ in pages for w in page_weeks]
                records[node_id] = fold(weeks, anchor_at, anchors[node_id]['stars'], pages=len(pages), has_more=pages[-1][2])
                diagnostic['new_entrants']['history_read'] += 1
            except GitHubError:
                diagnostic['new_entrants']['history_failed'] += 1
        if records:
            data.write_history(records)
            history.update(records)
    # 5. the known set, the meta, the windows, the diagnostic
    repos_doc.update({'threshold': threshold, 'updated_at': now.isoformat(), 'repos': dict(sorted(known.items()))})
    data.write_repos(repos_doc)
    first = meta.get('first_snapshot_date')
    first_snapshot = min(dt.date.fromisoformat(first), date) if first else (date if counts else None)
    if total is not None:
        meta.update({'last_total_count': total, 'last_total_count_at': now.isoformat(), 'last_total_count_source': total_source})
    meta.update({'first_snapshot_date': first_snapshot.isoformat() if first_snapshot else None, 'last_capture_date': date.isoformat() if counts else meta.get('last_capture_date')})
    data.write_meta(meta)
    page = None
    if counts:
        page = build_page(date, now.isoformat(), threshold, known, counts, data.snapshot_dates(), data.read_snapshot, history, first_snapshot)
        data.write_windows(date, page)
        diagnostic['windows'] = {'rows': page['row_count'], 'history_records': len(history), 'per_window': page['windows']}
    above = sum(1 for stars in counts.values() if stars > threshold)
    ratio = (above / total) if total else None
    diagnostic['coverage'] = {'rows_above_threshold': above, 'total_count': total, 'total_count_source': total_source, 'ratio': round(ratio, 4) if ratio is not None else None,
                              'floor': COVERAGE_FLOOR, 'ok': ratio is not None and ratio >= COVERAGE_FLOOR}
    diagnostic['exit_code'] = 2 if not counts else (0 if diagnostic['coverage']['ok'] else 3)
    diagnostic['seconds'] = round(time.monotonic() - started, 1)
    data.write_diagnostic(date, diagnostic)
    log(f'coverage: {above} rows above {threshold} vs total_count {total} ({total_source}) -> {diagnostic["coverage"]["ok"]}; exit {diagnostic["exit_code"]}')
    return diagnostic['exit_code'], diagnostic


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--data-dir', help='checkout of the data branch (read and written); with --dry-run only its repos/slices/meta are copied')
    parser.add_argument('--threshold', type=int, default=2000, help='repos with more stars than this (default 2000)')
    parser.add_argument('--workers', type=int, default=4, help='concurrent GraphQL queries, at most 4')
    parser.add_argument('--discovery-workers', type=int, default=DISCOVERY_WORKERS, help=f'search pages in flight at once, 1..8 (default {DISCOVERY_WORKERS}; the 30/min pacing always holds)')
    parser.add_argument('--deadline-minutes', type=float, default=60, help='discovery stops (partial) this long after the run started')
    parser.add_argument('--skip-discovery', action='store_true', help='snapshot the known set only')
    parser.add_argument('--sample', type=int, help='snapshot at most this many known repos (dry runs)')
    parser.add_argument('--max-slices', type=int, help='list at most this many discovery slices (dry runs; the rest are skipped)')
    parser.add_argument('--max-history', type=int, default=MAX_HISTORY_PER_DAY, help=f'read history for at most this many repos that lack it (default {MAX_HISTORY_PER_DAY})')
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
            for name in ('repos.json', 'slices.json', 'meta.json'):
                if (root / name).exists():
                    shutil.copy(root / name, tmp / name)
        root = tmp
    now = dt.datetime.fromisoformat(args.now.replace('Z', '+00:00')) if args.now else None
    def log_request(entry):
        with open(args.request_log, 'a') as handle:
            handle.write(json.dumps(entry, separators=(',', ':')) + '\n')

    gh = GitHub(token_from_environment(), request_log=log_request if args.request_log else None)
    code, diagnostic = run(gh, DataDir(root), args.threshold, now=now, workers=args.workers, deadline_minutes=args.deadline_minutes,
                           skip_discovery=args.skip_discovery, sample=args.sample, max_slices=args.max_slices, max_history=args.max_history,
                           discovery_workers=args.discovery_workers)
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
