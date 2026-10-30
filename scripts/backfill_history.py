"""One-time backfill of 365 days of per-day star history for the known set, on Neo's Mac with his gh token.

Pass 1 reads page 1 (the newest 30 weeks, enough for every window up to 180 days) for every repo; pass 2 reads page 2 for the
repos that have one. Before each batch of 100 repos their current counts are read by one GraphQL query and stored as the
anchor of their history (capture/history.py explains why; the batch's budget is waited for before the anchor, never after).
Every page is appended to the work dir's journal as soon as it arrives, so a killed or slept run resumes without re-reading a
finished repo; every request's timestamp goes to requests.log, the last hour of which a resumed run counts again, and the
sliding-hour budget --max-per-hour is never exceeded. --compact folds the journal into the data dir's history shards (it also
runs at the end of a complete pass 2).
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, random, signal, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from capture.github import GitHub, GitHubError, token_from_environment
from capture.history import anchored_reads, fold
from capture.store import DataDir

BATCH = 100


class Journal:
    """Append-only JSONL of page reads; one line per (node_id, page). A torn last line (kill -9 mid-write) is cut off on load,
    so the next line is not appended onto it."""
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries = {}
        if self.path.exists():
            raw = self.path.read_bytes()
            if raw and not raw.endswith(b'\n'):
                with open(self.path, 'r+b') as handle:
                    handle.truncate(raw.rfind(b'\n') + 1)
            for line in self.path.read_text().splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                self.entries[(entry['node_id'], entry['page'])] = entry

    def add(self, entry):
        with open(self.path, 'a') as handle:  # one open-write-close per line: nothing buffered when the process dies
            handle.write(json.dumps(entry, separators=(',', ':')) + '\n')
        self.entries[(entry['node_id'], entry['page'])] = entry

    def done(self, node_id, page):
        entry = self.entries.get((node_id, page))
        return entry is not None and entry['status'] != 'error'


def compact(journal, data):
    """Fold every repo with a good page 1 (plus its page 2) into the data dir's history shards. Returns the record count."""
    records = {}
    for (node_id, page), entry in journal.entries.items():
        if page != 1 or entry['status'] != 'ok':
            continue
        weeks = list(entry['weeks'])
        pages, has_more = 1, entry['has_next']
        second = journal.entries.get((node_id, 2))
        if second and second['status'] == 'ok':
            weeks += second['weeks']
            pages, has_more = 2, second['has_next']
        records[node_id] = fold(weeks, entry['anchor_at'], entry['anchor_stars'], pages=pages, has_more=has_more)
    if records:
        data.write_history(records)
    return len(records)


def backfill(gh, journal, targets, pages=2, stop=lambda: False, log=print, wall=None):
    """targets: {node_id: full_name}. Runs pass 1 then pass 2; returns stats. stop() is polled between requests."""
    wall = wall or (lambda: dt.datetime.now(dt.timezone.utc))
    stats = {'page1_read': 0, 'page2_read': 0, 'missing': 0, 'gone': 0, 'errors': 0, 'anchor_points': 0, 'stopped': False}
    todo = [i for i in sorted(targets) if not journal.done(i, 1)]
    log(f'pass 1: {len(todo)} of {len(targets)} repos need page 1')
    read = lambda full_name: gh.rest(f'/repos/{full_name}/stargazers/history', {'page': 1, 'per_page': 30})
    for start in range(0, len(todo), BATCH):
        if stop():
            stats['stopped'] = True
            return stats
        for node_id, status, anchor_at, anchor, result in anchored_reads(gh, todo[start:start + BATCH], read, wall, stats=stats):
            at = anchor_at.isoformat()
            if status == 'ok':
                weeks, headers = result
                journal.add({'node_id': node_id, 'page': 1, 'status': 'ok', 'full_name': anchor['full_name'], 'anchor_at': at, 'anchor_stars': anchor['stars'],
                             'fetched_at': wall().isoformat(), 'weeks': weeks, 'has_next': 'rel="next"' in (headers.get('link') or '')})
                stats['page1_read'] += 1
            elif status == 'missing':
                journal.add({'node_id': node_id, 'page': 1, 'status': 'missing', 'full_name': targets[node_id], 'at': at})
                stats['missing'] += 1
            elif status == 'failed':  # the anchor query failed, not the repo: 'error' is retried on resume
                journal.add({'node_id': node_id, 'page': 1, 'status': 'error', 'full_name': targets[node_id], 'at': at, 'error': 'the anchor query failed'})
                stats['errors'] += 1
            else:
                status = 'gone' if result.status == 404 else 'error'
                journal.add({'node_id': node_id, 'page': 1, 'status': status, 'full_name': anchor['full_name'], 'at': at, 'error': str(result)[:200]})
                stats['gone' if status == 'gone' else 'errors'] += 1
            if stop():
                stats['stopped'] = True
                return stats
        log(f'pass 1: {min(start + BATCH, len(todo))}/{len(todo)} ({stats})')
    if pages < 2:
        return stats
    todo = [i for i in sorted(targets) if journal.done(i, 1) and journal.entries[(i, 1)]['status'] == 'ok' and journal.entries[(i, 1)]['has_next'] and not journal.done(i, 2)]
    log(f'pass 2: {len(todo)} repos need page 2')
    for n, node_id in enumerate(todo, 1):
        if stop():
            stats['stopped'] = True
            return stats
        full_name = journal.entries[(node_id, 1)]['full_name']
        try:
            weeks, headers = gh.rest(f'/repos/{full_name}/stargazers/history', {'page': 2, 'per_page': 30})
            journal.add({'node_id': node_id, 'page': 2, 'status': 'ok', 'full_name': full_name, 'fetched_at': wall().isoformat(), 'weeks': weeks,
                         'has_next': 'rel="next"' in (headers.get('link') or '')})
            stats['page2_read'] += 1
        except GitHubError as error:
            status = 'gone' if error.status == 404 else 'error'
            journal.add({'node_id': node_id, 'page': 2, 'status': status, 'full_name': full_name, 'error': str(error)[:200]})
            stats['gone' if status == 'gone' else 'errors'] += 1
        if n % BATCH == 0 or n == len(todo):
            log(f'pass 2: {n}/{len(todo)} ({stats})')
    return stats


def restore_budget(gh, log_path):
    """Count the core requests of requests.log's last hour (an earlier run of this backfill) against the client's budget."""
    path = pathlib.Path(log_path)
    if not path.exists():
        return 0
    now, ages = gh.wall(), []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('method') == 'GET' and '/search/' not in entry.get('url', ''):
            ages.append(now - dt.datetime.fromisoformat(entry['at']).timestamp())
    return gh.restore_core(ages)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--data-dir', required=True, help='the data branch checkout: repos.json is the set; history/ shards are written by --compact')
    parser.add_argument('--work-dir', required=True, help='journal.jsonl and requests.log live here (outside the data branch)')
    parser.add_argument('--max-per-hour', type=int, default=3000, help='core requests per sliding hour, never exceeded (default 3000)')
    parser.add_argument('--pages', type=int, default=2, choices=(1, 2), help='history pages per repo (2 = 365 days)')
    parser.add_argument('--sample', type=int, help='a random subset of this many repos (acceptance runs)')
    parser.add_argument('--seed', type=int, default=20261029, help='seed for --sample')
    parser.add_argument('--repos-file', help='JSON {node_id: {full_name}} to backfill instead of repos.json')
    parser.add_argument('--compact', action='store_true', help='only fold the journal into the data dir and exit')
    args = parser.parse_args(argv)
    data = DataDir(args.data_dir)
    work = pathlib.Path(args.work_dir)
    journal = Journal(work / 'journal.jsonl')
    if args.compact:
        print(f'compacted {compact(journal, data)} repos into {data.root / "history"}')
        return 0
    source = json.loads(pathlib.Path(args.repos_file).read_text()) if args.repos_file else data.read_repos()['repos']
    targets = {node_id: item['full_name'] for node_id, item in source.items()}
    if args.sample:
        chosen = random.Random(args.seed).sample(sorted(targets), min(args.sample, len(targets)))
        targets = {i: targets[i] for i in chosen}
        (work / 'sample.json').write_text(json.dumps(targets, indent=1))
    def log_request(entry):
        with open(work / 'requests.log', 'a') as handle:
            handle.write(json.dumps(entry, separators=(',', ':')) + '\n')

    stopping = []
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    gh = GitHub(token_from_environment(), core_per_hour=args.max_per_hour, request_log=log_request)
    print(f'{restore_budget(gh, work / "requests.log")} core requests in the last hour of requests.log count against --max-per-hour')
    started = time.monotonic()
    stats = backfill(gh, journal, targets, pages=args.pages, stop=lambda: bool(stopping))
    stats['minutes'] = round((time.monotonic() - started) / 60, 1)
    stats['requests'] = dict(gh.counts)
    if stats['stopped']:
        print(json.dumps({'stopped': True, **stats}))
        print('stopped by signal; run again to resume')
        return 130
    stats['compacted'] = compact(journal, data)
    print(json.dumps(stats))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
