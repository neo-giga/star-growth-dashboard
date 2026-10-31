"""The daily run end to end on a synthetic set: discovery, snapshot, entrants' history, windows that flip from history to
snapshot as the ring deepens, every gain checked against a plain recomputation from the truth, search outages, coverage."""
import contextlib, datetime as dt, importlib.util, io, json, pathlib, shutil, subprocess, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture.github import GitHub
from capture.history import PACIFIC, WINDOWS, pacific_date
from capture.store import DataDir
from fake_github import FakeClock, make_world

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('daily_capture', ROOT / 'scripts/daily_capture.py')
daily_capture = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(daily_capture)
_spec = importlib.util.spec_from_file_location('build_windows', ROOT / 'scripts/build_windows.py')
build_windows = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(build_windows)
START = dt.datetime(2026, 3, 10, 5, 30, tzinfo=dt.timezone.utc)  # 21:30 PST on 2026-03-09, like the workflow's 05:30 UTC cron
THRESHOLD = 2000


def expected_gain(world, node_id, days, k, entered, instants):
    """The plain recomputation: snapshot diff when the repo was in the set N days ago, else the stars given from N x 24 h before
    the capture (that Pacific day pro rata) through the entry, plus the change since the entry count, within 0..stars."""
    now = instants[k]
    if k - days >= entered:
        return world.stars(node_id, now) - world.stars(node_id, instants[k - days])
    start, entry = now - dt.timedelta(days=days), instants[entered]
    first = pacific_date(start)
    day_start, day_end = (dt.datetime.combine(d, dt.time(), PACIFIC).astimezone(dt.timezone.utc) for d in (first, first + dt.timedelta(days=1)))
    head = world.day_count(node_id, first, entry) * (day_end - start) / (day_end - day_start)
    rest = world.gained(node_id, first + dt.timedelta(days=1), pacific_date(entry), entry)
    stars = world.stars(node_id, now)
    return max(0, min(stars, round(head + rest + stars - world.stars(node_id, entry))))


class DailyCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.world = make_world(repos=500, seed=11, now=START)
        self.data = DataDir(self.tmp / 'data')
        self.instants = {}

    def gh(self):
        clock = FakeClock()
        return GitHub('t', fetch=self.world.fetch, sleeper=clock.sleep, clock=clock, max_attempts=3)

    def run_day(self, k, **kw):
        self.world.now = START + dt.timedelta(days=k)
        self.instants[k] = self.world.now
        return daily_capture.run(self.gh(), self.data, THRESHOLD, now=self.world.now, wall=lambda: self.world.now, log=lambda *_: None, **kw)

    def above(self, instant):
        return {i for i in self.world.repos if self.world.stars(i, instant) > THRESHOLD}

    def test_ten_days_of_capture_match_the_recomputation_and_flip_sources_on_schedule(self):
        entered = {}
        for k in range(10):
            code, diag = self.run_day(k)
            self.assertEqual(code, 0, diag['coverage'])
            above = self.above(self.world.now)
            for node_id in above:
                entered.setdefault(node_id, k)
            page = json.loads((self.data.root / 'windows/latest.json').read_text())
            self.assertEqual(set(page['columns']['node_id']), above); self.assertEqual(page['row_count'], len(above))
            self.assertEqual(diag['coverage']['total_count'], len(above)); self.assertEqual(diag['coverage']['ratio'], 1.0)
            self.assertEqual(diag['discovery']['status'], 'complete'); self.assertEqual(diag['new_entrants']['history_failed'], 0)
            self.assertEqual(diag['new_entrants']['count'], len([i for i, e in entered.items() if e == k]))
            self.assertEqual(diag['snapshot']['asked'], len(entered) - diag['new_entrants']['count'])
            self.assertTrue(self.data.snapshot_path(pacific_date(self.world.now)).exists())
            self.assertEqual(page['first_snapshot_date'], pacific_date(START).isoformat())
            for index, window in enumerate(page['windows']):
                days = window['days']
                self.assertEqual(window['source'], 'snapshot' if k >= days else 'history', (k, days))
                self.assertEqual(window['exact_since'], (pacific_date(START) + dt.timedelta(days=days)).isoformat())
                if k >= days:
                    self.assertEqual(window['baseline_date'], pacific_date(self.instants[k - days]).isoformat())
                column = page['columns'][window['column']]
                for row, node_id in enumerate(page['columns']['node_id']):
                    want = expected_gain(self.world, node_id, days, k, entered[node_id], self.instants)
                    self.assertEqual(column[row], want, (k, days, node_id))
                    from_history = bool(page['columns']['history_mask'][row] & (1 << index))
                    self.assertEqual(from_history, want is not None and k - days < entered[node_id], (k, days, node_id))
                self.assertEqual(window['rows_snapshot'] + window['rows_history'] + window['rows_null'], page['row_count'])
                self.assertEqual(sum(1 for v in column if v is None), window['rows_null'])
            for row, node_id in enumerate(page['columns']['node_id']):
                self.assertEqual(page['columns']['stars'][row], self.world.stars(node_id, self.world.now))
                self.assertEqual(page['columns']['full_name'][row], self.world.repos[node_id]['full_name'])
        repos = self.data.read_repos()['repos']
        self.assertEqual(set(repos), set(entered))
        for node_id, k in entered.items():  # the known set carries no per-day field (the snapshot does): it only changes when a repo enters
            self.assertEqual(repos[node_id], {'full_name': self.world.repos[node_id]['full_name'], 'language': self.world.repos[node_id]['language'],
                                              'created_at': self.world.repos[node_id]['created_at'], 'first_seen': pacific_date(self.instants[k]).isoformat()})
        self.assertEqual(sorted(self.data.snapshot_dates()), [pacific_date(self.instants[k]) for k in range(10)])
        self.assertEqual(len(self.data.read_history_all()), len(entered))
        self.assertTrue((self.data.root / 'diagnostics/2026/2026-03-18.json').exists()); self.assertFalse((self.data.root / 'windows/2026').exists())
        meta = self.data.read_meta()
        self.assertEqual(meta['first_snapshot_date'], '2026-03-09'); self.assertEqual(meta['last_capture_date'], '2026-03-18')
        self.assertTrue(all(window['source'] == 'snapshot' for window in page['windows'][:2]))

    def test_the_branch_ignores_windows_and_build_windows_rebuilds_the_same_bytes(self):
        for k in range(3):
            self.run_day(k)
        latest = self.data.root / 'windows/latest.json'
        captured = latest.read_bytes()
        shutil.rmtree(self.data.root / 'windows')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_windows.main(['--data-dir', str(self.data.root)]), 0)
        self.assertEqual(latest.read_bytes(), captured)  # the publisher's rebuild is the capture's file, byte for byte
        git = lambda *a: subprocess.run(['git', '-C', str(self.data.root), *a], check=True, capture_output=True, text=True).stdout
        git('init', '-q'); git('add', '-A')
        staged = git('diff', '--cached', '--name-only').split()
        self.assertIn('.gitignore', staged); self.assertIn('snapshots/2026/2026-03-11.json', staged)
        self.assertFalse([name for name in staged if name.startswith('windows/')])  # committed daily it would cost ~141 MiB a year
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build_windows.main(['--data-dir', str(self.tmp / 'empty')]), 2)

    def test_entrant_history_is_read_within_the_anchor_bound(self):
        # Codex finding 2, daily path: 400 entrants x 2 pages at a second per request took ~13 min after one shared anchor.
        self.world.now = START
        def fetch(*args):
            self.world.now += dt.timedelta(seconds=1)
            return self.world.fetch(*args)
        clock = FakeClock()
        gh = GitHub('t', fetch=fetch, sleeper=clock.sleep, clock=clock, max_attempts=3)
        code, diag = daily_capture.run(gh, self.data, THRESHOLD, now=START, wall=lambda: self.world.now, log=lambda *_: None)
        history = self.data.read_history_all()
        self.assertGreater(len(history), 150); self.assertEqual(diag['new_entrants']['history_read'], len(history))
        last_read = {}
        for _, url, instant in self.world.requests:
            if 'stargazers/history' in url:
                last_read[url.split('/repos/')[1].split('/stargazers')[0]] = instant
        for node_id, record in history.items():
            age = (last_read[self.world.repos[node_id]['full_name']] - dt.datetime.fromisoformat(record['anchor_at'])).total_seconds()
            self.assertTrue(0 <= age <= 120, (node_id, age))  # capture.history.MAX_ANCHOR_AGE

    def test_a_search_outage_keeps_the_snapshot_and_reports_a_partial_discovery(self):
        self.run_day(0)
        self.world.search_down = True
        code, diag = self.run_day(1)
        # red (a partial capture never passes as complete), yet the day's files are all written for the commit
        self.assertEqual(code, 3); self.assertEqual(diag['discovery']['status'], 'failed')
        self.assertEqual(diag['coverage']['total_count_source'], 'previous'); self.assertTrue(diag['coverage']['ok'])
        self.assertEqual(diag['completeness']['reasons'], ['discovery failed', 'search total from an earlier capture'])
        self.assertTrue(self.data.snapshot_path(dt.date(2026, 3, 10)).exists())
        page = json.loads((self.data.root / 'windows/latest.json').read_text()); self.assertEqual(page['date'], '2026-03-10')
        self.assertEqual(page['windows'][0]['source'], 'snapshot')
        # half the slices dead: partial, with the slices recorded, the found repos kept, and the plan intact for tomorrow
        self.world.search_down = False
        plan_before = self.data.read_slices()
        for size in (100, 50, 30):  # every listing page 1 fails at every size: no slice lists, but total_count (per_page=1) still answers
            self.world.flaky[f'per_page={size}&page=1&'] = 10 ** 6
        code, diag = self.run_day(2)
        self.assertEqual(diag['discovery']['status'], 'failed'); self.assertEqual(diag['coverage']['total_count_source'], 'search')
        self.assertTrue(all(s['status'] == 'failed' for s in diag['discovery']['slices'])); self.assertEqual(self.data.read_slices(), plan_before)
        self.world.flaky.clear()
        self.world.max_per_page = 30; self.world.flaky['per_page=30&page=2&'] = 10 ** 6
        code, diag = self.run_day(3)
        self.assertEqual(diag['discovery']['status'], 'partial'); self.assertGreater(diag['discovery']['slices_failed'], 0)
        self.assertEqual(code, 3); self.assertEqual(diag['completeness']['reasons'], ['discovery partial'])

    def test_no_total_count_at_all_goes_red_but_still_writes_everything(self):
        self.run_day(0)
        (self.data.root / 'meta.json').unlink()
        self.world.search_down = True
        code, diag = self.run_day(1)
        self.assertEqual(code, 3); self.assertIsNone(diag['coverage']['total_count']); self.assertFalse(diag['coverage']['ok'])
        self.assertTrue(self.data.snapshot_path(dt.date(2026, 3, 10)).exists()); self.assertTrue((self.data.root / 'diagnostics/2026/2026-03-10.json').exists())

    def test_low_coverage_goes_red_after_writing_and_a_deleted_repo_is_counted_missing(self):
        self.run_day(0)
        doc = self.data.read_repos()
        keep = sorted(doc['repos'])[: len(doc['repos']) // 2]
        doc['repos'] = {i: doc['repos'][i] for i in keep}
        self.data.write_repos(doc)
        self.world.repos[keep[0]]['gone'] = True
        code, diag = self.run_day(1, skip_discovery=True)
        self.assertEqual(code, 3); self.assertEqual(diag['discovery']['status'], 'skipped'); self.assertEqual(diag['snapshot']['missing'], 1)
        self.assertLess(diag['coverage']['ratio'], 0.99); self.assertEqual(diag['coverage']['total_count_source'], 'previous')
        self.assertEqual(len(self.data.read_snapshot(dt.date(2026, 3, 10))), len(keep) - 1)
        page = json.loads((self.data.root / 'windows/latest.json').read_text()); self.assertNotIn(keep[0], page['columns']['node_id'])

    def test_a_repo_below_the_threshold_stays_known_but_leaves_the_page(self):
        self.run_day(0)
        node_id = sorted(self.data.read_repos()['repos'])[0]
        self.world.repos[node_id]['prefix'] = [0] * len(self.world.repos[node_id]['prefix']); self.world.repos[node_id]['arrivals'] = [0] * len(self.world.repos[node_id]['arrivals'])
        code, diag = self.run_day(1, skip_discovery=True)
        page = json.loads((self.data.root / 'windows/latest.json').read_text())
        self.assertNotIn(node_id, page['columns']['node_id']); self.assertIn(node_id, self.data.read_repos()['repos'])
        self.assertEqual(self.data.read_snapshot(dt.date(2026, 3, 10))[node_id], 0)

    def test_history_reads_are_capped_per_day_oldest_turn_first_and_the_rest_caught_up_later(self):
        code, diag = self.run_day(0, max_history=100)
        above0 = self.above(self.world.now)
        self.assertEqual((diag['new_entrants']['count'], diag['new_entrants']['history_pending'], diag['new_entrants']['history_read']), (len(above0), len(above0), 100))
        self.assertEqual(len(self.data.read_history_all()), 100)
        page = json.loads((self.data.root / 'windows/latest.json').read_text())
        self.assertEqual(sum(1 for v in page['columns']['gain_7d'] if v is None), len(above0) - 100)
        self.assertEqual(page['history']['pending_rows'], len(above0) - 100); self.assertEqual(sum(page['columns']['history_pending']), len(above0) - 100)
        self.assertEqual(page['windows'][1]['rows_pending'], len(above0) - 100)  # every null is pending history, none unexplained
        left0 = above0 - set(self.data.read_history_all())
        code, diag = self.run_day(1, max_history=100)
        entrants = self.above(self.world.now) - above0
        self.assertEqual(diag['new_entrants']['count'], len(entrants))
        history = self.data.read_history_all()
        self.assertFalse(entrants & set(history), "yesterday's leftovers have waited longer than today's entrants")
        self.assertEqual(len(history), 200); self.assertEqual(diag['new_entrants']['history_pending'], len(above0) + len(entrants) - 100)
        self.assertEqual(diag['history']['pending_after'], len(above0) + len(entrants) - 200)
        self.assertEqual(set(self.data.read_pending()), (left0 | entrants) - set(history))
        for k in range(2, 6):
            self.run_day(k, max_history=100)
        self.assertEqual(self.data.read_repos()['repos'].keys(), self.data.read_history_all().keys())
        self.assertEqual(self.data.read_pending(), {})

    def test_an_empty_known_set_without_discovery_writes_nothing_and_exits_2(self):
        code, diag = self.run_day(0, skip_discovery=True)
        self.assertEqual(code, 2); self.assertEqual(self.data.snapshot_dates(), []); self.assertIsNone(diag['windows'])

    def test_dry_run_writes_a_temporary_directory_and_a_diagnostic_without_touching_the_data_dir(self):
        self.run_day(0)
        before = sorted(str(p.relative_to(self.data.root)) for p in self.data.root.rglob('*') if p.is_file())
        diagnostic = self.tmp / 'diag.json'
        out = io.StringIO()
        with mock.patch.object(daily_capture, 'GitHub', lambda token, **kw: self.gh()), mock.patch.object(daily_capture, 'token_from_environment', lambda: 'x'), contextlib.redirect_stdout(out):
            code = daily_capture.main(['--dry-run', '--data-dir', str(self.data.root), '--diagnostic', str(diagnostic), '--sample', '50', '--max-slices', '2',
                                       '--now', (START + dt.timedelta(days=1)).isoformat()])
        self.assertEqual(code, 3, out.getvalue())  # a 50-repo sample is far under 99% coverage: red, but everything is written
        after = sorted(str(p.relative_to(self.data.root)) for p in self.data.root.rglob('*') if p.is_file())
        self.assertEqual(before, after)
        diag = json.loads(diagnostic.read_text())
        self.assertTrue(diag['dry_run']); self.assertEqual(diag['snapshot']['asked'], 50); self.assertIn(diag['discovery']['status'], ('complete', 'partial'))
        root = pathlib.Path(diag['data_dir']); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertNotEqual(root, self.data.root)
        self.assertTrue((root / 'snapshots/2026/2026-03-10.json').exists()); self.assertTrue((root / 'windows/latest.json').exists())
        self.assertTrue((root / 'diagnostics/2026/2026-03-10.json').exists()); self.assertIn(f'dry run wrote {root}', out.getvalue())
        self.assertEqual((diag['new_entrants']['history_pending'], diag['new_entrants']['history_read']), (50, 50))  # the copy has no history shards
        self.assertGreaterEqual(diag['requests']['requests'], 1 + 1 + sum(s['pages'] for s in diag['discovery']['slices']) + 1 + 50)

    def test_cli_argument_rules(self):
        with self.assertRaises(SystemExit):
            daily_capture.main([])
        with self.assertRaises(SystemExit):
            daily_capture.main(['--data-dir', 'x', '--workers', '5'])


if __name__ == '__main__':
    unittest.main()
