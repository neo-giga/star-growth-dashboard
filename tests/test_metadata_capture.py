"""The metadata step (capture.metadata): queue order, the query/points/deadline stops (deferred, never failed), partial GraphQL
errors, no shard rewrite for unchanged content, no regression of a newer check, the file contract, and the count path left
exactly as it is when the step throws, waits or is disabled."""
import datetime as dt, importlib.util, json, pathlib, shutil, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture import metadata
from capture.github import GitHub
from capture.store import DataDir, shard_of
from fake_github import FakeClock, make_world

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('daily_capture', ROOT / 'scripts/daily_capture.py')
daily_capture = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(daily_capture)
DAY = dt.date(2026, 11, 1)
WALL = dt.datetime(2026, 11, 2, 6, 0, tzinfo=dt.timezone.utc).timestamp()


class FakeMeta:
    """GraphQL answers for the metadata and rateLimit queries; everything else goes to `inner` (the count world)."""
    def __init__(self, inner=None, limit=5000, remaining=5000, cost=1):
        self.inner = inner
        self.meta = {}  # node_id -> {description, topics} or 'gone'
        self.limit, self.remaining, self.cost = limit, remaining, cost
        self.reset_in = 1800
        self.errors = {}  # node_id -> error type at its path
        self.pathless = False
        self.fail = 0  # answer this many metadata queries with HTTP 422
        self.asked = []  # the ids of every metadata query, in order
        self.clock, self.latency = None, 0  # a FakeClock advanced by `latency` s per metadata query

    def rate(self, cost):
        self.remaining -= cost
        reset = dt.datetime.fromtimestamp(WALL + self.reset_in, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        return {'cost': cost, 'limit': self.limit, 'remaining': self.remaining, 'resetAt': reset}

    def fetch(self, method, url, body, headers):
        query = json.loads(body) if body else {}
        if url.endswith('/graphql') and query['query'] == metadata.RATE_QUERY:
            return 200, {}, json.dumps({'data': {'rateLimit': self.rate(0)}}).encode()
        if url.endswith('/graphql') and 'repositoryTopics' in query['query']:
            if self.fail:
                self.fail -= 1
                return 422, {}, b'{"message":"bad"}'
            ids = query['variables']['ids']
            self.asked.append(ids)
            if self.clock:
                self.clock.t += self.latency
            nodes, errors = [], []
            for index, node_id in enumerate(ids):
                item = self.meta.get(node_id, 'gone')
                if node_id in self.errors:
                    errors.append({'type': self.errors[node_id], 'path': ['nodes', index], 'message': 'x'})
                if item == 'gone' or node_id in self.errors:
                    nodes.append(None)
                else:
                    nodes.append({'id': node_id, 'description': item['description'],
                                  'repositoryTopics': {'totalCount': len(item['topics']), 'nodes': [{'topic': {'name': t}} for t in item['topics'][:metadata.TOPICS]]}})
            if self.pathless:
                errors.append({'type': 'INTERNAL', 'message': 'something went wrong'})
            return 200, {}, json.dumps({'data': {'rateLimit': self.rate(self.cost), 'nodes': nodes}, **({'errors': errors} if errors else {})}).encode()
        return self.inner.fetch(method, url, body, headers)


class MetadataStepTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fake = FakeMeta()
        self.ids = [f'R_{n:05d}' for n in range(250)]
        for n, node_id in enumerate(self.ids):
            self.fake.meta[node_id] = {'description': f'repo {n}', 'topics': [f't{n % 7}', 'common']}
        self.known = {i: {'first_seen': '2026-10-01'} for i in self.ids}
        self.clock = FakeClock(tick=0.5)

    def sleep(self, seconds):
        self.clock.sleep(seconds)
        if seconds >= self.fake.reset_in:  # the hour turned over
            self.fake.remaining = self.fake.limit

    def gh(self):
        return GitHub('t', fetch=self.fake.fetch, sleeper=self.sleep, clock=self.clock, wall=lambda: WALL, max_attempts=2)

    def run_step(self, day=DAY, cohort=None, **kw):
        return metadata.capture(self.gh(), self.root, self.ids if cohort is None else cohort, self.known, day, wall=lambda: WALL, log=lambda *_: None, **kw)

    def shards(self):
        return {p.name: p.read_bytes() for p in sorted((self.root / 'metadata/shards').glob('*.json'))}

    def checked(self):
        return json.loads((self.root / 'metadata/checked.json').read_text())

    def test_the_files_match_the_contract(self):
        self.fake.meta[self.ids[0]] = {'description': None, 'topics': [f'x{k}' for k in range(25)]}
        report = self.run_step()
        self.assertEqual((report['checked'], report['failed'], report['gone'], report['deferred']), (250, 0, 0, 0))
        self.assertEqual(report['queries'], 3); self.assertEqual(report['coverage'], 1.0); self.assertEqual(report['oldest_checked'], DAY.isoformat())
        records = {}
        for name, raw in self.shards().items():
            doc = json.loads(raw)
            self.assertEqual(set(doc), {'schema', 'repos'}); self.assertEqual(doc['schema'], 1)
            for node_id, record in doc['repos'].items():
                self.assertEqual(f'{shard_of(node_id)}.json', name)
                self.assertEqual(set(record), {'description', 'topics', 'topics_total', 'changed', 'status'})
                records[node_id] = record
        self.assertEqual(set(records), set(self.ids))
        self.assertEqual(records[self.ids[0]], {'description': None, 'topics': [f'x{k}' for k in range(20)], 'topics_total': 25, 'changed': DAY.isoformat(), 'status': 'ok'})
        self.assertEqual(records[self.ids[3]], {'description': 'repo 3', 'topics': ['t3', 'common'], 'topics_total': 2, 'changed': DAY.isoformat(), 'status': 'ok'})
        self.assertEqual(self.checked(), {'schema': 1, 'checked': {i: DAY.isoformat() for i in self.ids}})
        self.assertEqual(set(report['budget']), {'queries_used', 'queries_limit', 'points_per_hour_plan', 'points_per_hour_reported', 'points_per_hour_effective',
                                                 'remaining_at_end', 'reserve'})
        self.assertEqual(report['budget']['points_per_hour_reported'], 5000); self.assertEqual(report['budget']['points_per_hour_effective'], 1000)

    def test_queue_never_fetched_newest_first_then_oldest_checked_and_outsiders_skipped(self):
        known = {'A': {'first_seen': '2026-10-01'}, 'B': {'first_seen': '2026-10-31'}, 'C': {'first_seen': '2026-10-31'}, 'D': {}, 'E': {}, 'F': {}, 'G': {}, 'OUT': {}}
        checked = {'D': '2026-09-20', 'E': '2026-09-01', 'F': '2026-10-15', 'G': '2026-10-02', 'OUT': '2026-01-01'}
        never, due = metadata.queue(['A', 'B', 'C', 'D', 'E', 'F', 'G'], known, checked, DAY, recheck_days=30)
        self.assertEqual(never, ['B', 'C', 'A']); self.assertEqual(due, ['E', 'D', 'G'])  # F (checked 17 days ago) is not due; OUT is not in the cohort
        self.known.update({self.ids[0]: {'first_seen': '2026-10-31'}})
        self.run_step(cohort=self.ids[:150], max_queries=1)
        self.assertEqual(self.fake.asked[0][0], self.ids[0]); self.assertEqual(self.fake.asked[0][1:], self.ids[1:100])

    def test_query_budget_stops_with_the_rest_deferred_and_the_next_day_continues(self):
        report = self.run_step(max_queries=1)
        self.assertEqual((report['queries'], report['checked'], report['failed'], report['deferred'], report['stopped']), (1, 100, 0, 150, 'query budget'))
        self.assertEqual(report['coverage'], 0.4)
        report = self.run_step(day=DAY + dt.timedelta(days=1), max_queries=1)
        self.assertEqual(self.fake.asked[1], self.ids[100:200]); self.assertEqual(report['deferred'], 50)

    def test_zero_queries_disables_the_step_without_a_request(self):
        report = self.run_step(max_queries=0)
        self.assertEqual((report['queries'], report['checked'], report['deferred']), (0, 0, 250)); self.assertEqual(self.fake.asked, [])
        self.assertFalse((self.root / 'metadata').exists())

    def test_the_deadline_defers_untouched_and_keeps_what_was_read(self):
        self.fake.clock, self.fake.latency = self.clock, 10
        report = self.run_step(deadline_seconds=15)  # 10 s a query: two start before the deadline, the third may not
        self.assertEqual((report['checked'], report['failed'], report['deferred']), (200, 0, 50)); self.assertIn('deadline', report['stopped'])
        self.assertEqual(len(self.checked()['checked']), report['checked'])

    def test_points_plan_waits_for_the_reset_inside_the_deadline_and_stops_past_it(self):
        self.fake.remaining = 4000 + 50 + 1  # limit 5000, plan 1000: one point usable above the reserve
        report = self.run_step(deadline_seconds=3600)
        self.assertEqual(report['checked'], 250); self.assertTrue(any(s >= 1800 for s in self.clock.sleeps))
        self.setUp()
        self.fake.remaining = 4000 + 50 + 1
        report = self.run_step(deadline_seconds=600)
        self.assertEqual((report['checked'], report['failed'], report['deferred']), (100, 0, 150)); self.assertIn('points', report['stopped'])
        self.setUp()
        self.fake.limit, self.fake.remaining = 300, 300  # GitHub's limit is the lower one: it applies
        report = self.run_step(deadline_seconds=600)
        self.assertEqual(report['budget']['points_per_hour_effective'], 300); self.assertEqual(report['checked'], 250)

    def test_partial_errors_like_the_count_path(self):
        self.run_step()
        before = self.shards()
        a, b, c = self.ids[0], self.ids[1], self.ids[2]
        self.fake.errors = {a: 'NOT_FOUND', b: 'FORBIDDEN'}
        self.fake.meta[c] = 'gone'  # null without an error: gone
        later = DAY + dt.timedelta(days=40)
        report = self.run_step(day=later)
        self.assertEqual((report['gone'], report['failed']), (2, 1))
        checked = self.checked()['checked']
        self.assertEqual(checked[b], DAY.isoformat())  # errored: not checked today, the old record stays
        self.assertEqual(checked[a], later.isoformat())
        records = metadata.Store(self.root).read_shards()
        self.assertEqual(records[a], {'description': 'repo 0', 'topics': ['t0', 'common'], 'topics_total': 2, 'changed': later.isoformat(), 'status': 'gone'})
        self.assertEqual(records[b]['changed'], DAY.isoformat()); self.assertEqual(records[b]['status'], 'ok')
        self.assertEqual(records[c]['status'], 'gone')
        self.assertEqual({k for k in self.shards() if self.shards()[k] != before[k]}, {f'{shard_of(a)}.json', f'{shard_of(c)}.json'})
        self.fake.errors, self.fake.pathless = {}, True  # an error without a path: every null node unresolved, never gone
        self.fake.meta[self.ids[5]] = 'gone'
        report = self.run_step(day=later + dt.timedelta(days=40))
        self.assertEqual(metadata.Store(self.root).read_shards()[self.ids[5]]['status'], 'ok'); self.assertGreaterEqual(report['failed'], 1)

    def test_failed_queries_leave_records_and_three_in_a_row_stop(self):
        self.fake.fail = 1
        report = self.run_step()
        self.assertEqual((report['failed'], report['checked']), (100, 150))
        self.assertEqual(metadata.queue(self.ids, self.known, self.checked()['checked'], DAY)[0], self.ids[:100])  # still never fetched, first tomorrow
        self.setUp(); self.fake.fail = 5
        report = self.run_step()
        self.assertEqual((report['queries'], report['failed'], report['checked']), (3, 250, 0)); self.assertIn('in a row', report['stopped'])
        self.assertFalse((self.root / 'metadata').exists())

    def test_unchanged_content_rewrites_no_shard(self):
        self.run_step()
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (self.root / 'metadata/shards').glob('*.json')}
        later = DAY + dt.timedelta(days=31)
        report = self.run_step(day=later)
        self.assertEqual((report['checked'], report['changed'], report['shards_written']), (250, 0, 0))
        self.assertEqual({p: (p.stat().st_mtime_ns, p.read_bytes()) for p in before}, before)
        self.assertEqual(set(self.checked()['checked'].values()), {later.isoformat()})
        self.fake.meta[self.ids[7]] = {'description': 'new words', 'topics': ['t0', 'common']}
        report = self.run_step(day=later + dt.timedelta(days=31))
        self.assertEqual((report['changed'], report['shards_written']), (1, 1))
        record = metadata.Store(self.root).read_shards()[self.ids[7]]
        self.assertEqual((record['description'], record['changed']), ('new words', (later + dt.timedelta(days=31)).isoformat()))

    def test_same_day_rerun_reads_nothing_again_and_never_regresses_a_newer_check(self):
        self.run_step()
        self.assertEqual(self.run_step()['asked'], 0)  # all checked today: not due
        newer = DAY + dt.timedelta(days=5)
        doc = self.checked(); doc['checked'][self.ids[0]] = newer.isoformat()
        (self.root / 'metadata/checked.json').write_text(json.dumps(doc))
        self.fake.meta[self.ids[0]] = {'description': 'stale', 'topics': []}
        self.run_step(day=DAY + dt.timedelta(days=40), cohort=self.ids[:1], recheck_days=1)  # due, but the stored check is newer
        self.run_step(day=DAY + dt.timedelta(days=2), cohort=self.ids[:1], recheck_days=-10)  # an older run date than the stored check
        self.assertEqual(self.checked()['checked'][self.ids[0]], (DAY + dt.timedelta(days=40)).isoformat())
        self.assertEqual(metadata.Store(self.root).read_shards()[self.ids[0]]['description'], 'stale')
        self.fake.meta[self.ids[0]] = {'description': 'older run', 'topics': []}
        self.run_step(day=DAY + dt.timedelta(days=3), cohort=self.ids[:1], recheck_days=-100)
        self.assertEqual(metadata.Store(self.root).read_shards()[self.ids[0]]['description'], 'stale')


class CountPathUntouchedTests(unittest.TestCase):
    """daily_capture.run with the metadata step working, throwing, waiting past its deadline or disabled: same count files,
    same completeness, same exit code; only the diagnostic's metadata block differs."""
    START = dt.datetime(2026, 3, 10, 5, 30, tzinfo=dt.timezone.utc)

    def capture(self, **kw):
        tmp = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        world = make_world(repos=300, seed=5, now=self.START)
        fake = FakeMeta(world)
        for node_id in world.repos:
            fake.meta[node_id] = {'description': node_id, 'topics': ['a']}
        clock = FakeClock()
        gh = GitHub('t', fetch=fake.fetch, sleeper=clock.sleep, clock=clock, max_attempts=3)
        code, diag = daily_capture.run(gh, DataDir(tmp), 2000, now=self.START, wall=lambda: self.START, log=lambda *_: None, **kw)
        files = {str(p.relative_to(tmp)): p.read_bytes() for p in tmp.rglob('*') if p.is_file() and 'metadata' not in p.parts and 'diagnostics' not in p.parts}
        saved = json.loads(next(tmp.glob('diagnostics/*/*.json')).read_text())
        return code, diag, files, saved, tmp

    def test_metadata_outcomes_never_touch_the_counts(self):
        code, diag, files, saved, tmp = self.capture(metadata_queries=0)
        self.assertEqual(code, 0); self.assertEqual(diag['metadata']['status'], 'disabled')
        for kw in ({}, {'metadata_deadline_minutes': 0}):
            other = self.capture(**kw)
            self.assertEqual(other[0], code); self.assertEqual(other[2], files); self.assertEqual(other[1]['completeness'], diag['completeness'])
        ok = self.capture()
        self.assertEqual(ok[1]['metadata']['status'], 'ok'); self.assertEqual(ok[1]['metadata']['coverage'], 1.0)
        self.assertEqual(ok[3]['metadata'], ok[1]['metadata'])  # the committed diagnostic carries the block
        self.assertEqual(ok[3]['captured_at'], ok[1]['captured_at'])
        self.assertTrue((ok[4] / 'metadata/checked.json').exists())
        late = self.capture(metadata_deadline_minutes=0)
        self.assertIn('deadline', late[1]['metadata']['stopped']); self.assertEqual(late[1]['metadata']['failed'], 0)
        with mock.patch.object(daily_capture.metadata, 'capture', side_effect=RuntimeError('boom')):
            broken = self.capture()
        self.assertEqual(broken[0], code); self.assertEqual(broken[2], files); self.assertEqual(broken[1]['completeness'], diag['completeness'])
        self.assertEqual(broken[1]['metadata'], {'status': 'failed', 'error': "RuntimeError('boom')"})
        self.assertEqual(broken[3]['metadata']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
