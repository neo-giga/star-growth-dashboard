"""The inclusive 1,000-star set (threshold 999): the boundary, an old fully measured cohort joined by a new one whose history is
pending, the durable fair history queue, failed count batches, same-day reruns and the gate's pending-history rule."""
import datetime as dt, importlib.util, json, pathlib, shutil, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture.discovery import initial_plan, total_count
from capture.github import GitHub
from capture.store import DataDir
from capture.windows import build_page
from fake_github import FakeClock, World, make_world
from publish import stage as st

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('daily_capture', ROOT / 'scripts/daily_capture.py')
daily_capture = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(daily_capture)
_spec = importlib.util.spec_from_file_location('build_windows', ROOT / 'scripts/build_windows.py')
build_windows = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(build_windows)
START = dt.datetime(2026, 3, 10, 5, 30, tzinfo=dt.timezone.utc)


def page_of(data):
    return json.loads((data.root / 'windows/latest.json').read_text())


class BoundaryTests(unittest.TestCase):
    def test_999_excluded_1000_1001_2000_2001_included_at_threshold_999_and_2000_keeps_its_meaning(self):
        stars = {'R_999': 999, 'R_1000': 1000, 'R_1001': 1001, 'R_2000': 2000, 'R_2001': 2001}
        repos = {i: {'full_name': f'o/{i}', 'language': None, 'created_at': '2020-01-01T00:00:00Z'} for i in stars}
        def rows(threshold):
            page = build_page(dt.date(2026, 3, 9), START.isoformat(), threshold, repos, stars, [], lambda d: None, {}, None)
            st.validate_windows(page)
            return sorted(page['columns']['node_id'])
        self.assertEqual(rows(999), ['R_1000', 'R_1001', 'R_2000', 'R_2001'])
        self.assertEqual(rows(2000), ['R_2001'])  # the old threshold still means "more than 2,000"
        self.assertEqual(initial_plan(999)[0]['lo'], 1000)
        page = build_page(dt.date(2026, 3, 9), START.isoformat(), 999, repos, stars, [], lambda d: None, {}, None)
        page['columns']['stars'][-1] = 999
        with self.assertRaisesRegex(ValueError, 'at or below the threshold 999'):
            st.validate_windows(page)

    def test_search_total_and_discovery_use_the_same_inclusive_bound(self):
        world = World(START)
        for n, count in enumerate((999, 1000, 1001, 2000, 2001)):
            world.add(f'R_{count}', f'o/r{count}', [0] * 700 + [count] + [0] * 99)
        gh = GitHub('t', fetch=world.fetch, sleeper=FakeClock().sleep, clock=FakeClock(), max_attempts=2)
        self.assertEqual(total_count(gh, 999), 4); self.assertEqual(total_count(gh, 2000), 1)
        self.assertIn('stars%3A%3E999+fork%3Atrue', world.requests[0][1])


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.world = make_world(repos=500, seed=11, now=START)
        self.data = DataDir(self.tmp / 'data')
        self.fail_graphql = set()  # node IDs whose GraphQL batch answers 502 every time
        self.fail_history = set()  # full names whose history answers 404

    def fetch(self, method, url, body, headers):
        if url.endswith('/graphql') and self.fail_graphql & set(json.loads(body)['variables']['ids']):
            return 502, {}, b'<html>502</html>'
        if any(f'/repos/{name}/stargazers/history' in url for name in self.fail_history):
            return 404, {}, b'{"message":"Not Found"}'
        return self.world.fetch(method, url, body, headers)

    def run_day(self, k, threshold, **kw):
        self.world.now = START + dt.timedelta(days=k)
        clock = FakeClock()
        gh = GitHub('t', fetch=self.fetch, sleeper=clock.sleep, clock=clock, max_attempts=2, core_per_hour=900)
        return daily_capture.run(gh, self.data, threshold, now=self.world.now, wall=lambda: self.world.now, log=lambda *_: None, **kw)

    def old_cohort(self, days=3):
        for k in range(days):
            code, _ = self.run_day(k, 2000)
            self.assertEqual(code, 0)
        return {i: r for i, r in self.data.read_history_all().items()}

    def test_the_old_cohort_keeps_its_history_and_the_new_one_shows_in_every_window_as_pending(self):
        old = self.old_cohort()
        shards = {p.name: p.read_bytes() for p in (self.data.root / 'history').glob('*.json')}
        code, diag = self.run_day(3, 999, max_history=0)
        self.assertEqual(code, 0, diag['completeness'])
        page = page_of(self.data)
        ids = page['columns']['node_id']
        above = {i for i in self.world.repos if self.world.stars(i, self.world.now) > 999}
        self.assertEqual(set(ids), above)
        new = above - set(old)
        self.assertTrue(new and any(1000 <= self.world.stars(i, self.world.now) <= 2000 for i in new))
        self.assertEqual({p.name: p.read_bytes() for p in (self.data.root / 'history').glob('*.json')}, shards)  # established histories untouched
        self.assertEqual(set(self.data.read_pending()), new)
        pending = dict(zip(ids, page['columns']['history_pending']))
        self.assertEqual({i for i, flag in pending.items() if flag}, new); self.assertEqual(page['history']['pending_rows'], len(new))
        for window in page['windows']:  # the same rows in every window; a pending row's unknown gain is null, never zero
            column = page['columns'][window['column']]
            self.assertEqual(len(column), page['row_count'])
            for node_id, gain in zip(ids, column):
                if node_id in new and window['days'] > 0:
                    self.assertIsNone(gain, (node_id, window['column']))
                if node_id in old:
                    self.assertIsNotNone(gain, (node_id, window['column']))
            self.assertEqual(window['rows_pending'], len(new)); self.assertEqual(window['rows_null'], len(new))
        diagnostic = json.loads((self.data.root / 'diagnostics/2026/2026-03-12.json').read_text())
        problems, measured = st.gate(st.validate_windows(page), diagnostic, self.world.now + dt.timedelta(hours=1))
        self.assertEqual(problems, []); self.assertEqual(measured['pending_rows'], len(new))
        self.assertGreater(measured['null_share']['gain_7d'], 0.01); self.assertEqual(measured['null_share_not_pending']['gain_7d'], 0.0)
        self.assertEqual(page['capture']['status'], 'complete'); self.assertEqual(diag['completeness']['rows'], len(above))
        # the publisher's rebuild from committed files keeps the pending marks and the capture status, byte for byte
        captured = (self.data.root / 'windows/latest.json').read_bytes()
        shutil.rmtree(self.data.root / 'windows'); build_windows.build(DataDir(self.data.root))
        self.assertEqual((self.data.root / 'windows/latest.json').read_bytes(), captured)
        # the next day, the bounded daily lane reads part of the queue, oldest turn first, and leaves every other record alone
        code, diag = self.run_day(4, 999, max_history=10)
        history = self.data.read_history_all()
        self.assertEqual(len(history), len(old) + 10); self.assertEqual({i: history[i] for i in old}, old)
        self.assertEqual(diag['history']['read'], 10); self.assertEqual(diag['history']['pending_after'], len(new) + diag['new_entrants']['count'] - 10)
        self.assertEqual(page_of(self.data)['history']['pending_rows'], diag['history']['pending_after'])

    def test_a_previously_complete_record_that_goes_missing_is_not_pending_and_the_gate_refuses(self):
        self.old_cohort(2)
        code, _ = self.run_day(2, 999, max_history=0)
        lost = sorted(self.data.read_history_all())[:20]
        for path in (self.data.root / 'history').glob('*.json'):
            shard = json.loads(path.read_text())
            path.write_text(json.dumps({k: v for k, v in shard.items() if k not in lost}))
        build_windows.build(DataDir(self.data.root))
        page = page_of(self.data)
        flags = dict(zip(page['columns']['node_id'], page['columns']['history_pending']))
        self.assertFalse(any(flags.get(i) for i in lost))
        diagnostic = json.loads((self.data.root / 'diagnostics/2026/2026-03-11.json').read_text())
        problems, _ = st.gate(st.validate_windows(page), diagnostic, self.world.now + dt.timedelta(hours=1))
        self.assertTrue(any('gain_90d is null' in p for p in problems), problems)
        # and the next capture repairs them first, without queueing them as pending
        code, diag = self.run_day(3, 999, max_history=30)
        self.assertEqual(diag['history']['repair'], len([i for i in lost if self.world.stars(i, self.world.now) > 999]))
        self.assertTrue(set(lost) <= set(self.data.read_history_all())); self.assertFalse(set(lost) & set(self.data.read_pending()))

    def test_the_queue_is_durable_and_fair_a_failing_repo_goes_behind_the_others(self):
        self.run_day(0, 999, max_history=0)
        queue = self.data.read_pending()
        order = sorted(queue, key=lambda i: daily_capture.history_turn(queue[i], i))
        bad = order[0]
        self.fail_history.add(self.world.repos[bad]['full_name'])
        code, diag = self.run_day(1, 999, max_history=5, skip_discovery=True)
        queue = self.data.read_pending()
        self.assertEqual(queue[bad]['attempts'], 1); self.assertEqual(queue[bad]['last_status'], 'error 404'); self.assertEqual(diag['history']['failed'], 1)
        self.assertEqual(diag['history']['read'], 4); self.assertTrue(set(order[1:5]) <= set(self.data.read_history_all()))
        code, diag = self.run_day(2, 999, max_history=5, skip_discovery=True)
        self.assertTrue(set(order[5:10]) <= set(self.data.read_history_all()))  # the failing repo waits behind the ones that waited longer
        self.assertEqual(self.data.read_pending()[bad]['attempts'], 1)
        self.assertNotEqual(code, 2)

    def test_a_failed_count_batch_is_unread_not_observed_and_the_gate_refuses(self):
        self.run_day(0, 999, max_history=0)
        victim = sorted(self.data.read_repos()['repos'])[0]
        self.fail_graphql.add(victim)
        code, diag = self.run_day(1, 999, max_history=0, skip_discovery=True)
        self.assertEqual(code, 3); self.assertEqual(diag['counts']['known_unread'], 100); self.assertIn(victim, diag['counts']['known_unread_ids'])
        self.assertEqual(diag['completeness']['status'], 'partial'); self.assertIn('100 known repositories unread', diag['completeness']['reasons'])
        self.assertIn('discovery skipped', diag['completeness']['reasons'])
        self.assertNotIn(victim, self.data.read_snapshot(dt.date(2026, 3, 10)))  # no stand-in count from another day
        self.assertIn(victim, self.data.read_repos()['repos'])  # its metadata is kept
        problems, measured = st.gate(st.validate_windows(page_of(self.data)), diag, self.world.now + dt.timedelta(hours=1))
        self.assertTrue(any('100 known and 0 new repositories unread' in p for p in problems), problems); self.assertEqual(measured['unread'], {'known': 100, 'new': 0})

    def test_a_same_day_rerun_keeps_the_earlier_good_counts_it_cannot_read(self):
        self.run_day(0, 999, max_history=0)
        first = dict(self.data.read_snapshot(dt.date(2026, 3, 9)))
        victim = sorted(first)[0]
        self.fail_graphql.add(victim)
        self.world.now = START + dt.timedelta(minutes=50)  # 23:20 PDT: still the same Pacific day
        self.assertEqual(daily_capture.pacific_date(self.world.now), dt.date(2026, 3, 9))
        clock = FakeClock()
        gh = GitHub('t', fetch=self.fetch, sleeper=clock.sleep, clock=clock, max_attempts=2)
        data = DataDir(self.data.root)  # a fresh process: nothing cached
        code, diag = daily_capture.run(gh, data, 999, now=self.world.now, wall=lambda: self.world.now, log=lambda *_: None, max_history=0, skip_discovery=True)
        second = DataDir(self.data.root).read_snapshot(dt.date(2026, 3, 9))
        self.assertEqual(set(second), set(first)); self.assertEqual(second[victim], first[victim])
        self.assertEqual(diag['counts']['carried_from_earlier_run'], 100); self.assertEqual(diag['counts']['known_unread'], 0)
        self.assertIn(victim, page_of(self.data)['columns']['node_id'])

    def test_an_earlier_total_stands_in_only_for_the_same_threshold(self):
        self.old_cohort(1)
        self.world.search_down = True
        code, diag = self.run_day(1, 999, max_history=0)
        self.assertIsNone(diag['coverage']['total_count']); self.assertEqual(code, 3)  # a >2000 total is no denominator for >999
        self.assertIn('no search total today', diag['completeness']['reasons'])
        meta = self.data.read_meta(); self.assertEqual(meta['last_total_count_threshold'], 2000)


if __name__ == '__main__':
    unittest.main()
