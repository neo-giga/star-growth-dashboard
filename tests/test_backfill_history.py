"""The backfill: page 1 for every repo first, then page 2; anchors per batch; a journal that resumes after a kill without
re-reading a finished repo; the per-hour budget never exceeded; compaction into history shards the daily windows use."""
import datetime as dt, importlib.util, io, json, pathlib, shutil, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture.github import GitHub
from capture.history import WINDOWS, fold, pacific_date
from capture.store import DataDir, shard_of
from capture.windows import build_page
from fake_github import FakeClock, make_world

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('backfill_history', ROOT / 'scripts/backfill_history.py')
backfill_history = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(backfill_history)
NOW = dt.datetime(2026, 3, 10, 18, 0, tzinfo=dt.timezone.utc)  # 10:00 PST: the anchor day is well under way


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.world = make_world(repos=260, seed=5, now=NOW)
        self.data = DataDir(self.tmp / 'data')
        self.targets = {i: r['full_name'] for i, r in self.world.repos.items()}
        self.clock = FakeClock(tick=0.5)

    def gh(self, **kw):
        return GitHub('t', fetch=self.world.fetch, sleeper=self.clock.sleep, clock=self.clock, wall=lambda: 1_700_000_000 + self.clock.t, max_attempts=3, **kw)

    def history_urls(self):
        return [u for _, u, _ in self.world.requests if 'stargazers/history' in u]

    def test_pass_1_then_pass_2_with_anchors_and_every_page_read_once(self):
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        stats = backfill_history.backfill(self.gh(), journal, self.targets, log=lambda *_: None, wall=lambda: self.world.now)
        urls = self.history_urls()
        self.assertEqual(len(urls), len(set(urls)))
        page1 = [u for u in urls if 'page=1&' in u]; page2 = [u for u in urls if 'page=2&' in u]
        self.assertEqual(len(page1), 260); self.assertEqual(urls[:260], page1)  # every page 1 before any page 2
        self.assertEqual(stats['page1_read'], 260); self.assertEqual(stats['page2_read'], len(page2)); self.assertGreater(len(page2), 0); self.assertLess(len(page2), 260)
        self.assertEqual(stats['anchor_points'], 3); self.assertFalse(stats['stopped'])
        for node_id in self.targets:
            entry = journal.entries[(node_id, 1)]
            self.assertEqual(entry['anchor_stars'], self.world.stars(node_id, NOW)); self.assertEqual(entry['status'], 'ok')
            self.assertEqual((node_id, 2) in journal.entries, entry['has_next'])
        self.assertEqual(backfill_history.compact(journal, self.data), 260)
        history = self.data.read_history_all()
        self.assertEqual(len(history), 260)
        for node_id, record in history.items():
            self.assertEqual(record['through'], '2026-03-10'); self.assertEqual(record['anchor_stars'], self.world.stars(node_id, NOW))
            if not record['has_more']:
                self.assertEqual(sum(record['days']), record['anchor_stars'])  # the whole history was read: it adds up to the count
            else:
                self.assertLess(sum(record['days']), record['anchor_stars']); self.assertEqual(record['pages'], 2); self.assertEqual(len(record['days']), 60 * 7 - 4)
        self.assertEqual(len(list((self.data.root / 'history').glob('*.json'))), len({shard_of(i) for i in history}))

    def test_a_killed_run_resumes_without_re_reading_or_missing_a_repo(self):
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        stats = backfill_history.backfill(self.gh(), journal, self.targets, stop=lambda: len(self.history_urls()) >= 137, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertTrue(stats['stopped']); self.assertEqual(stats['page1_read'], 137)
        self.world.repos['R_00003']['gone'] = True  # deleted between the runs: recorded as gone, never retried as an error
        resumed = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        stats = backfill_history.backfill(self.gh(), resumed, self.targets, stop=lambda: len(self.history_urls()) >= 300, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertTrue(stats['stopped'])
        stats = backfill_history.backfill(self.gh(), backfill_history.Journal(self.tmp / 'work/journal.jsonl'), self.targets, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertFalse(stats['stopped'])
        urls = self.history_urls()
        self.assertEqual(len(urls), len(set(urls)), 'a page was read twice')
        final = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        self.assertEqual({i for i, p in final.entries if p == 1}, set(self.targets))
        self.assertEqual({i for i, p in final.entries if p == 2}, {i for i in self.targets if final.entries[(i, 1)]['status'] == 'ok' and final.entries[(i, 1)]['has_next']})
        gone = final.entries[('R_00003', 1)]
        self.assertEqual(gone['status'], 'ok')  # read before the kill
        if gone['has_next']:
            self.assertEqual(final.entries[('R_00003', 2)]['status'], 'gone')
        # a torn last line (kill -9 mid-write) is ignored and the entry re-read
        with open(self.tmp / 'work/journal.jsonl', 'a') as handle:
            handle.write('{"node_id": "R_00005", "page": 2, "stat')
        torn = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        self.assertEqual(len(torn.entries), len(final.entries))

    def test_errors_are_retried_on_the_next_run_and_the_budget_is_never_exceeded(self):
        self.world.flaky['repo7/'] = 10 ** 6
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        log = []
        gh = self.gh(core_per_hour=40, request_log=log.append)
        stats = backfill_history.backfill(gh, journal, {i: self.targets[i] for i in sorted(self.targets)[:120]}, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertEqual(stats['errors'], 1); self.assertEqual(journal.entries[('R_00007', 1)]['status'], 'error')
        self.assertFalse(journal.done('R_00007', 1))
        core = sorted(dt.datetime.fromisoformat(e['at']).timestamp() for e in log if 'stargazers/history' in e['url'])
        for i in range(len(core)):
            self.assertLessEqual(sum(1 for t in core[i:] if t < core[i] + 3600), 40, 'more than 40 core requests in one hour')
        self.assertGreater(len(core), 40); self.assertTrue(any(s > 3000 for s in self.clock.sleeps))
        self.world.flaky.clear()
        stats = backfill_history.backfill(self.gh(), backfill_history.Journal(self.tmp / 'work/journal.jsonl'), {i: self.targets[i] for i in sorted(self.targets)[:120]}, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertEqual(stats['page1_read'], 1); self.assertEqual(stats['errors'], 0)

    def test_the_daily_windows_computed_from_compacted_history_match_the_truth(self):
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        backfill_history.backfill(self.gh(), journal, self.targets, log=lambda *_: None, wall=lambda: self.world.now)
        backfill_history.compact(journal, self.data)
        capture = NOW + dt.timedelta(hours=11, minutes=30)  # 21:30 PST the same day: the first daily capture after the backfill
        self.world.now = capture
        date = pacific_date(capture)
        counts = {i: self.world.stars(i, capture) for i in self.targets}
        page = build_page(date, capture.isoformat(), 2000, {i: {'full_name': n} for i, n in self.targets.items()}, counts, [date], lambda d: counts, self.data.read_history_all(), date)
        self.assertTrue(all(w['source'] == 'history' for w in page['windows']))
        for row, node_id in enumerate(page['columns']['node_id']):
            for days in WINDOWS:
                want = counts[node_id] - self.world.stars(node_id, capture - dt.timedelta(days=days))  # the truth over the N x 24 h before the capture
                self.assertLessEqual(abs(page['columns'][f'gain_{days}d'][row] - want), 1, (node_id, days))  # the start day pro rata: rounding only
        self.assertEqual(page['columns']['history_mask'], [127] * page['row_count'])

    def test_cli_writes_the_sample_the_journal_and_the_request_log_then_compacts(self):
        self.data.write_repos({'threshold': 2000, 'updated_at': None, 'repos': {i: {'full_name': n} for i, n in self.targets.items()}})
        work = self.tmp / 'work'
        out = io.StringIO()
        with mock.patch.object(backfill_history, 'GitHub', lambda token, **kw: self.gh(**kw)), mock.patch.object(backfill_history, 'token_from_environment', lambda: 'x'), \
             mock.patch.object(backfill_history.signal, 'signal', lambda *_: None), mock.patch('sys.stdout', out):
            code = backfill_history.main(['--data-dir', str(self.data.root), '--work-dir', str(work), '--sample', '30', '--max-per-hour', '500'])
        self.assertEqual(code, 0, out.getvalue())
        sample = json.loads((work / 'sample.json').read_text()); self.assertEqual(len(sample), 30)
        lines = [json.loads(l) for l in (work / 'requests.log').read_text().splitlines()]
        self.assertEqual(sum(1 for l in lines if 'stargazers/history' in l['url']), sum(1 for u in self.history_urls()))
        self.assertTrue(all('at' in l and l['status'] == 200 for l in lines))
        self.assertEqual(len(self.data.read_history_all()), 30); self.assertEqual(json.loads(out.getvalue().splitlines()[-1])['compacted'], 30)
        with mock.patch('sys.stdout', io.StringIO()) as again:
            self.assertEqual(backfill_history.main(['--data-dir', str(self.data.root), '--work-dir', str(work), '--compact']), 0)
        self.assertIn('compacted 30 repos', again.getvalue())

    def test_a_throttle_wait_never_sits_between_an_anchor_and_its_history_read(self):
        # Codex finding 2: the world moves while the backfill waits for its budget, so a stale anchor would count stars twice.
        clock = FakeClock()
        at = lambda: NOW + dt.timedelta(seconds=clock.t - 1000)
        def fetch(*args):
            self.world.now = at()
            return self.world.fetch(*args)
        gh = GitHub('t', fetch=fetch, sleeper=clock.sleep, clock=clock, wall=lambda: at().timestamp(), max_attempts=3, core_per_hour=120)
        targets = {i: self.targets[i] for i in sorted(self.targets)[:150]}
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        stats = backfill_history.backfill(gh, journal, targets, pages=1, log=lambda *_: None, wall=at)
        self.assertEqual(stats['page1_read'], 150); self.assertTrue(any(s > 3000 for s in clock.sleeps))  # the second batch waited for the budget
        moved = 0
        for node_id in targets:
            entry = journal.entries[(node_id, 1)]
            anchor_at = dt.datetime.fromisoformat(entry['anchor_at'])
            record = fold(entry['weeks'], anchor_at, entry['anchor_stars'], pages=1, has_more=entry['has_next'])
            through = dt.date.fromisoformat(record['through'])
            self.assertEqual(record['days'][-1], self.world.day_count(node_id, through, anchor_at), node_id)  # the anchor day holds no star given after the anchor
            if not entry['has_next']:
                self.assertEqual(sum(record['days']), entry['anchor_stars'], node_id)
            moved += self.world.day_count(node_id, through, anchor_at) != self.world.day_count(node_id, through, anchor_at + dt.timedelta(hours=1))
        self.assertGreater(moved, 10)  # stars do arrive within the hour a stale anchor would have waited

    def test_a_read_that_outlasts_the_anchor_bound_is_anchored_and_read_again(self):
        clock = FakeClock()
        at = lambda: NOW + dt.timedelta(seconds=clock.t - 1000)
        def fetch(*args):
            self.world.now = at()
            return self.world.fetch(*args)
        gh = GitHub('t', fetch=fetch, sleeper=clock.sleep, clock=clock, wall=lambda: at().timestamp(), max_attempts=8)
        targets = {i: self.targets[i] for i in sorted(self.targets)[:20]}
        slow = targets[sorted(targets)[5]]
        self.world.flaky[slow + '/'] = 7  # 7 answers of 502: 197 s of retry waits inside one read
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        stats = backfill_history.backfill(gh, journal, targets, pages=1, log=lambda *_: None, wall=at)
        self.assertEqual(stats['page1_read'], 20)
        reads = {}
        for _, url, instant in self.world.requests:
            if 'stargazers/history' in url:
                reads[url.split('/repos/')[1].split('/stargazers')[0]] = instant
        for node_id, full_name in targets.items():
            entry = journal.entries[(node_id, 1)]
            age = (reads[full_name] - dt.datetime.fromisoformat(entry['anchor_at'])).total_seconds()
            self.assertTrue(0 <= age <= 120, (full_name, age))  # capture.history.MAX_ANCHOR_AGE
        self.assertEqual(stats.get('anchor_queries'), 2)  # the batch's anchor, then the rest of the batch again after the slow read

    def test_a_resumed_run_counts_the_last_hour_of_requests_log_against_the_budget(self):
        log_path = self.tmp / 'work/requests.log'; log_path.parent.mkdir(parents=True)
        def log_request(entry):  # what main() writes
            with open(log_path, 'a') as handle:
                handle.write(json.dumps(entry) + '\n')
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        backfill_history.backfill(self.gh(core_per_hour=40, request_log=log_request), journal, self.targets, stop=lambda: len(self.history_urls()) >= 40,
                                  log=lambda *_: None, wall=lambda: self.world.now)
        first_run = [json.loads(l) for l in log_path.read_text().splitlines()]
        self.assertEqual(sum(1 for e in first_run if 'stargazers/history' in e['url']), 40)  # stopped at the cap
        gh = self.gh(core_per_hour=40, request_log=log_request)  # resumed at once
        self.assertEqual(backfill_history.restore_budget(gh, log_path), 40)
        backfill_history.backfill(gh, backfill_history.Journal(self.tmp / 'work/journal.jsonl'), self.targets, stop=lambda: len(self.history_urls()) >= 45,
                                  log=lambda *_: None, wall=lambda: self.world.now)
        oldest = min(dt.datetime.fromisoformat(e['at']) for e in first_run if 'stargazers/history' in e['url'])
        resumed = [dt.datetime.fromisoformat(e['at']) for e in [json.loads(l) for l in log_path.read_text().splitlines()][len(first_run):]]
        self.assertTrue(resumed); self.assertGreaterEqual(min(resumed), oldest + dt.timedelta(hours=1))  # 0 requests until the oldest aged out

    def test_a_failed_anchor_query_is_retried_on_resume_and_only_a_null_node_is_missing(self):
        targets = {i: self.targets[i] for i in sorted(self.targets)[:150]}
        self.world.flaky['/graphql'] = 3  # the first batch's anchor query fails after its 3 attempts
        self.world.repos[sorted(targets)[120]]['gone'] = True  # a real null node in the second batch
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        stats = backfill_history.backfill(self.gh(), journal, targets, pages=1, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertEqual((stats['errors'], stats['missing'], stats['page1_read']), (100, 1, 49))
        self.assertFalse(any(journal.done(i, 1) for i in sorted(targets)[:100])); self.assertTrue(journal.done(sorted(targets)[120], 1))
        stats = backfill_history.backfill(self.gh(), backfill_history.Journal(self.tmp / 'work/journal.jsonl'), targets, pages=1, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertEqual((stats['page1_read'], stats['errors'], stats['missing']), (100, 0, 0))

    def test_a_week_rollover_between_page_1_and_page_2_leaves_no_gap(self):
        # Codex finding 9 is not real: weeks are listed newest first, so a new week shifts page 2 one week newer (it overlaps
        # page 1 by a week, which fold de-duplicates); a gap cannot appear. The record keeps the truth day by day.
        saturday = dt.datetime(2026, 3, 14, 18, 0, tzinfo=dt.timezone.utc)
        self.world.now = saturday
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        backfill_history.backfill(self.gh(), journal, self.targets, pages=1, log=lambda *_: None, wall=lambda: self.world.now)
        self.world.now = saturday + dt.timedelta(days=3)  # Tuesday: the newest week is now the one of Sunday 03-15
        journal = backfill_history.Journal(self.tmp / 'work/journal.jsonl')
        stats = backfill_history.backfill(self.gh(), journal, self.targets, log=lambda *_: None, wall=lambda: self.world.now)
        self.assertGreater(stats['page2_read'], 100)
        backfill_history.compact(journal, self.data)
        for node_id, record in self.data.read_history_all().items():
            anchor_at, start = dt.datetime.fromisoformat(record['anchor_at']), dt.date.fromisoformat(record['start'])
            truth = [self.world.day_count(node_id, start + dt.timedelta(days=i), anchor_at) for i in range(len(record['days']))]
            self.assertEqual(record['days'], truth, node_id)
            if record['has_more']:
                self.assertGreaterEqual((dt.date.fromisoformat(record['through']) - start).days, 365)

    def test_a_torn_tail_is_cut_before_the_next_line_is_appended(self):
        path = self.tmp / 'work/journal.jsonl'
        backfill_history.Journal(path).add({'node_id': 'a', 'page': 1, 'status': 'ok'})
        with open(path, 'a') as handle:
            handle.write('{"node_id": "b", "page": 1, "stat')  # kill -9 mid-write
        torn = backfill_history.Journal(path)
        self.assertEqual(set(torn.entries), {('a', 1)})
        torn.add({'node_id': 'c', 'page': 1, 'status': 'ok'})
        self.assertEqual(set(backfill_history.Journal(path).entries), {('a', 1), ('c', 1)})

if __name__ == '__main__':
    unittest.main()
