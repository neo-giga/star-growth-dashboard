"""A capture is published only when it is whole: GraphQL partial errors are unread (not gone), a partial discovery or a stale
denominator never passes, incomplete or failing histories are retried in turn without starving the queue, the history deadline
stops every request, and an interrupted rerun's files are never paired with an earlier run's diagnostic."""
import datetime as dt, importlib.util, json, pathlib, re, shutil, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture.github import GitHub
from capture import snapshot
from capture.store import DataDir
from fake_github import FakeClock, make_world
from publish import stage as st
from publish.agent_feed import record

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('daily_capture', ROOT / 'scripts/daily_capture.py')
daily_capture = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(daily_capture)
_spec = importlib.util.spec_from_file_location('build_windows', ROOT / 'scripts/build_windows.py')
build_windows = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(build_windows)
START = dt.datetime(2026, 3, 10, 5, 30, tzinfo=dt.timezone.utc)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.world = make_world(repos=500, seed=11, now=START)
        self.data = DataDir(self.tmp / 'data')
        self.partial = {}  # node ID -> GraphQL error type answered at its path (with a null node) inside an HTTP 200
        self.fail_history = set()
        self.clock = FakeClock()
        self.on_request = None

    def fetch(self, method, url, body, headers):
        if self.on_request:
            self.on_request(url)
        if any(f'/repos/{name}/stargazers/history' in url for name in self.fail_history):
            return 404, {}, b'{"message":"Not Found"}'
        status, response_headers, raw = self.world.fetch(method, url, body, headers)
        if url.endswith('/graphql') and self.partial:
            ids = json.loads(body)['variables']['ids']
            answer = json.loads(raw)
            errors = []
            for index, node_id in enumerate(ids):
                if node_id in self.partial:
                    answer['data']['nodes'][index] = None
                    errors.append({'type': self.partial[node_id], 'path': ['nodes', index], 'message': 'x'})
            if errors:
                answer['errors'] = errors
                raw = json.dumps(answer).encode()
        return status, response_headers, raw

    def gh(self):
        self.clock = FakeClock()
        return GitHub('t', fetch=self.fetch, sleeper=self.clock.sleep, clock=self.clock, max_attempts=2, core_per_hour=900)

    def run_day(self, k, minutes=0, **kw):
        self.world.now = START + dt.timedelta(days=k, minutes=minutes)
        return daily_capture.run(self.gh(), self.data, 999, now=self.world.now, wall=lambda: self.world.now, log=lambda *_: None, **kw)

    def gate(self):
        page = json.loads((self.data.root / 'windows/latest.json').read_text())
        diagnostic = json.loads((self.data.root / 'diagnostics' / page['date'][:4] / f'{page["date"]}.json').read_text())
        return st.gate(st.validate_windows(page), diagnostic, self.world.now + dt.timedelta(hours=1))


class GraphQLPartialErrorTests(Harness):
    def test_classify_only_not_found_or_errorless_nulls_are_gone(self):
        data = {'nodes': [{'id': 'A', 'nameWithOwner': 'o/a', 'stargazerCount': 5}, None, None, None]}
        errors = [{'type': 'NOT_FOUND', 'path': ['nodes', 1]}, {'type': 'SERVICE_UNAVAILABLE', 'path': ['nodes', 2]}]
        rows, missing, unresolved = snapshot.classify(['A', 'B', 'C', 'D'], data, errors)
        self.assertEqual((list(rows), missing, unresolved), (['A'], ['B', 'D'], ['C']))
        self.assertEqual(snapshot.classify(['A', 'B'], {'nodes': [None, None]}, [{'type': 'TIMEOUT', 'message': 'no path'}])[2], ['A', 'B'])

    def test_an_http_200_with_errored_nodes_is_unread_red_and_refused_not_dropped(self):
        self.run_day(0, max_history=0)
        known = sorted(self.data.read_repos()['repos'])
        victim, gone = known[3], known[4]
        self.partial = {victim: 'SERVICE_UNAVAILABLE', gone: 'NOT_FOUND'}
        code, diag = self.run_day(1, max_history=0)
        self.assertEqual(code, 3)
        self.assertEqual(diag['counts']['known_unread'], 1); self.assertEqual(diag['counts']['known_unread_ids'], [victim])
        self.assertEqual(diag['counts']['inaccessible'], 1)  # NOT_FOUND is GitHub's answer for a deleted repo: gone, not unread
        self.assertEqual(diag['snapshot']['retry']['asked'], 1)  # the unresolved ID was asked again
        problems, _ = self.gate()
        self.assertTrue(any('1 known and 0 new repositories unread' in p for p in problems), problems)


class SuccessGateTests(Harness):
    def test_a_partial_discovery_with_full_coverage_is_red_and_never_published(self):
        self.run_day(0, max_history=400)
        with mock.patch.object(daily_capture, 'discover', side_effect=RuntimeError('search down')):
            code, diag = self.run_day(1, max_history=0)
        self.assertGreaterEqual(diag['coverage']['ratio'], 0.99)  # the known set alone clears the coverage floor
        self.assertEqual(code, 3); self.assertEqual(diag['completeness']['status'], 'partial')
        self.assertTrue((self.data.root / 'snapshots/2026/2026-03-10.json').exists())  # the day's files are still written
        problems, _ = self.gate()
        self.assertTrue(any(p.startswith('the capture was partial: discovery failed') for p in problems), problems)

    def test_a_legacy_diagnostic_without_complete_discovery_or_todays_total_is_refused(self):
        self.run_day(0, max_history=400)
        page = json.loads((self.data.root / 'windows/latest.json').read_text())
        legacy = {'date': page['date'], 'captured_at': page['generated_at'], 'coverage': {'total_count': page['row_count'], 'total_count_source': 'previous'},
                  'discovery': {'status': 'partial'}, 'snapshot': {'failed_queries': 0}}
        problems, _ = st.gate(st.validate_windows(page), legacy, self.world.now + dt.timedelta(hours=1))
        self.assertIn('the capture was partial: discovery partial; no search total that day', problems)
        legacy.update(coverage={'total_count': page['row_count'], 'total_count_source': 'search'}, discovery={'status': 'complete'})
        self.assertEqual(st.gate(st.validate_windows(page), legacy, self.world.now + dt.timedelta(hours=1))[0], [])


class HistoryRecoveryTests(Harness):
    def test_an_incomplete_record_keeps_the_repo_queued_pending_and_retried(self):
        self.run_day(0, max_history=0)
        queue = self.data.read_pending()
        node_id = sorted(queue, key=lambda i: daily_capture.history_turn(queue[i], i))[0]
        real = daily_capture.read_pages  # the read stops after page 1 with older weeks left unread (has_more)
        with mock.patch.object(daily_capture, 'read_pages', lambda gh, name, pages=2: [(p, w, True) for p, w, _ in real(gh, name, pages=1)]):
            code, diag = self.run_day(1, max_history=1, skip_discovery=True)
        self.assertEqual(diag['history']['incomplete'], 1)
        self.assertIn(node_id, self.data.read_pending()); self.assertEqual(self.data.read_pending()[node_id]['last_status'], 'incomplete')
        self.assertIn(node_id, self.data.read_history_all())  # the partial work is kept
        page = json.loads((self.data.root / 'windows/latest.json').read_text())
        row = page['columns']['node_id'].index(node_id)
        self.assertEqual(page['columns']['history_pending'][row], 2); self.assertEqual(page['history']['pending_retrying'], 1)
        self.assertEqual(record(page, row)['history_state'], 'retrying')

    def test_persistently_failing_repairs_never_take_every_daily_read(self):
        self.run_day(0, max_history=400)
        complete = sorted(self.data.read_history_all())[:12]
        for path in (self.data.root / 'history').glob('*.json'):
            shard = json.loads(path.read_text())
            path.write_text(json.dumps({k: v for k, v in shard.items() if k not in complete}))
        self.fail_history = {self.world.repos[i]['full_name'] for i in complete}
        before = len(self.data.read_pending())
        for n in range(30):  # entrants waiting in the pending queue
            self.world.add(f'R_E{n:03}', f'e/r{n}', [0] * 700 + [1500] + [0] * 99)
        code, diag = self.run_day(1, max_history=10)
        self.assertEqual(diag['history']['repair'], 5); self.assertEqual(diag['history']['failed'], 5)
        self.assertEqual(diag['history']['read'], 5)  # the pending queue still moves
        self.assertEqual(len(self.data.read_pending()), before + 30 - 5)
        code, diag = self.run_day(2, max_history=10)
        repair = self.data.read_repair()
        self.assertEqual(len(repair), 12); self.assertEqual(diag['history']['repair_retrying'], 10)
        self.assertEqual(sum(1 for e in repair.values() if e.get('attempts') == 1), 10)  # the next five took their turn, not the same five again
        self.assertFalse(set(complete) & set(self.data.read_pending()))  # formerly complete records are never pending


class HistoryDeadlineTests(Harness):
    def test_no_history_request_starts_after_the_deadline_and_the_rest_is_deferred(self):
        self.run_day(0, max_history=0)
        starts = []
        def slow(url):
            if '/stargazers/history' in url:
                starts.append(self.clock.t)
                self.clock.t += 30  # a slow request: 30 s each, no pacing wait between them
        self.on_request = slow
        code, diag = self.run_day(1, max_history=100, skip_discovery=True, history_deadline_minutes=5)
        report = diag['history']
        self.assertIn('deadline', report['stopped']); self.assertEqual(report['failed'], 0)
        self.assertGreater(report['deferred'], 0); self.assertLessEqual(len(starts), 11)  # 300 s / 30 s, plus the one in flight
        self.assertEqual(report['read'] + report['deferred'] + report['incomplete'] + report['unread'], report['selected'])
        self.assertTrue((self.data.root / 'diagnostics/2026/2026-03-10.json').exists())  # and the run still wrote everything


class GenerationTests(Harness):
    def test_an_interrupted_same_day_rerun_is_never_published_with_the_earlier_diagnostic(self):
        self.run_day(0, max_history=400)
        self.assertEqual(self.gate()[0], [])
        with mock.patch.object(DataDir, 'read_history_all', side_effect=RuntimeError('killed')), self.assertRaises(RuntimeError):
            self.run_day(0, minutes=40, max_history=0)  # rewrites today's snapshot, then dies before repos/meta/diagnostic
        build_windows.build(DataDir(self.data.root))
        problems, measured = self.gate()
        self.assertTrue(any('an interrupted run' in p for p in problems), problems)
        self.assertTrue(any(p.startswith('inputs from another capture') for p in problems), problems)
        # the unfinished counts were kept, and the next complete rerun publishes again
        code, diag = self.run_day(0, minutes=50, max_history=0)
        self.assertEqual(code, 0); self.assertEqual(self.gate()[0], [])


class StaticTextTests(unittest.TestCase):
    def test_the_page_and_llms_txt_do_not_promise_a_minimum_the_data_does_not_say(self):
        html = (ROOT / 'index.html').read_text()
        self.assertIn('id="min-stars"', html)
        self.assertNotRegex(html, r'(above|at least) [\d,]+ stars')
        self.assertNotRegex((ROOT / 'llms.txt').read_text().split('\n', 1)[0], r'[\d,]{4,} stars')

    def test_the_readme_leads_with_agents_and_says_topic_queries_are_not_live(self):
        readme = (ROOT / 'README.md').read_text()
        lead = readme.split('\n## ', 1)[0]
        self.assertIn('machine-readable GitHub repository discovery dataset for agents', lead)
        for harness in ('Claude Code', 'Muse', 'Grok'):
            self.assertIn(harness, lead)
        self.assertLess(lead.index('api/v2/catalog.json'), lead.index('https://neo-giga.github.io/star-growth-dashboard/**'))  # feed first, page second
        self.assertRegex(lead, r'secondary human explorer'); self.assertRegex(lead, r'(?s)Not live yet.*topic or task queries.*not implemented')
        differs = readme.split('## How this differs from existing solutions', 1)[1].split('\n## ', 1)[0]
        for needle in ('https://www.star-history.com/', 'https://www.star-history.com/compare', 'https://www.star-history.com/blog/new-github-star-history-api',
                       'Star Map', 'embeds', 'ranking', 'neither has exclusive or more'):
            self.assertIn(needle, differs)
        self.assertIn('A partial day is not published', readme)
        for doc in ('llms.txt', 'docs/agent-feed.md'):
            text = (ROOT / doc).read_text()
            self.assertIn('agents', text.split('\n\n', 2)[0] + text.split('\n\n', 2)[1]); self.assertRegex(text, r'planned follow-on, not live')


if __name__ == '__main__':
    unittest.main()
