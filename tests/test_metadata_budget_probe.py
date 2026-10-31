"""THROWAWAY probe (branch agent-metadata-probe, never merged): the workflow's branch guard and read-only permissions, the three
queries' fields, the fixture, and the probe's reporting on a fake transport (no network)."""
import contextlib, io, json, pathlib, re, sys, tempfile, unittest, unittest.mock
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from capture.snapshot import QUERY as CAPTURE_QUERY
from scripts import metadata_budget_probe as probe

WORKFLOW = (ROOT / '.github/workflows/daily-capture.yml').read_text()
FIXTURE = ROOT / 'tests/fixtures/metadata-probe-ids.json'
CAPTURE_IF = "github.repository == 'neo-giga/star-growth-dashboard' && github.ref == 'refs/heads/main'"
PROBE_IF = ("github.repository == 'neo-giga/star-growth-dashboard' && github.ref == 'refs/heads/agent-metadata-probe' "
            "&& github.event_name == 'workflow_dispatch'")


def jobs():
    body = WORKFLOW.split('\njobs:\n', 1)[1]
    parts = re.split(r'^  ([\w-]+):\n', body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def eligible(condition, repository, ref, event):
    """Evaluate an `a == 'x' && b == 'y'` job condition (the only form these guards use)."""
    values = {'github.repository': repository, 'github.ref': ref, 'github.event_name': event}
    clauses = [c.strip() for c in condition.split('&&')]
    for clause in clauses:
        name, literal = re.fullmatch(r"([\w.]+) == '([^']*)'", clause).groups()
        if values[name] != literal:
            return False
    return True


class GuardTests(unittest.TestCase):
    def test_guards_are_exact(self):
        j = jobs()
        self.assertEqual(list(j), ['capture', 'metadata-budget-probe'])
        self.assertIn(f'    if: {CAPTURE_IF}\n', j['capture'])  # unchanged main-only guard
        self.assertIn(f'    if: {PROBE_IF}\n', j['metadata-budget-probe'])
        self.assertNotIn('agent-metadata-probe', j['capture'])

    def test_only_the_probe_is_eligible_on_the_branch_and_only_the_capture_on_main(self):
        conds = {name: re.search(r'^    if: (.*)$', text, re.M)[1] for name, text in jobs().items()}
        repo = 'neo-giga/star-growth-dashboard'
        cases = {('refs/heads/agent-metadata-probe', 'workflow_dispatch'): {'metadata-budget-probe'},
                 ('refs/heads/main', 'workflow_dispatch'): {'capture'}, ('refs/heads/main', 'schedule'): {'capture'},
                 ('refs/heads/agent-metadata-probe', 'schedule'): set(), ('refs/heads/inclusive-1000', 'workflow_dispatch'): set()}
        for (ref, event), expected in cases.items():
            self.assertEqual({n for n, c in conds.items() if eligible(c, repo, ref, event)}, expected, (ref, event))
        self.assertEqual({n for n, c in conds.items() if eligible(c, 'someone/fork', 'refs/heads/agent-metadata-probe', 'workflow_dispatch')}, set())

    def test_probe_job_is_read_only(self):
        job = re.sub(r'^ *#.*\n', '', jobs()['metadata-budget-probe'], flags=re.M)  # the keys and scripts, not the comments
        self.assertRegex(job, r'\n    permissions:\n      contents: read\n    steps:\n')
        self.assertEqual(re.findall(r'^      [\w-]+: (?:read|write)', job, re.M), ['      contents: read'])
        for forbidden in ('write', 'git push', 'git commit', 'data-branch', 'origin data', 'secrets.', 'pages', 'environment:', 'daily_capture.py'):
            self.assertNotIn(forbidden, job.lower() if forbidden == 'pages' else job)
        self.assertIn('persist-credentials: false', job)
        self.assertIn('GH_TOKEN: ${{ github.token }}', job)
        self.assertIn('uses: actions/upload-artifact@v4', job)
        self.assertEqual(re.findall(r'uses: ([\w/-]+)@', job), ['actions/checkout', 'actions/upload-artifact'])


class QueryTests(unittest.TestCase):
    def test_three_read_only_queries_extend_the_capture_fields(self):
        capture_fields = re.search(r'\.\.\. on Repository \{ (.*) \} \}\n', CAPTURE_QUERY)[1]
        self.assertEqual(capture_fields, 'id nameWithOwner stargazerCount createdAt primaryLanguage { name }')
        q = probe.QUERIES
        self.assertEqual(list(q), ['A_scalar', 'B_description', 'C_description_topics'])
        fields = {k: re.search(r'\.\.\. on Repository \{ (.*) \} \}\n', v)[1] for k, v in q.items()}
        self.assertEqual(fields['A_scalar'], capture_fields)
        self.assertEqual(fields['B_description'], capture_fields + ' description')
        self.assertEqual(fields['C_description_topics'], capture_fields + ' description repositoryTopics(first: 5) '
                         '{ totalCount nodes { topic { name } } pageInfo { hasNextPage } }')
        for text in q.values():
            self.assertTrue(text.startswith('query($ids: [ID!]!) {'))
            self.assertIn('rateLimit { cost limit remaining resetAt nodeCount }', text)
            self.assertIn('nodes(ids: $ids)', text)
            self.assertNotIn('mutation', text)
        self.assertEqual(probe.MAX_ATTEMPTS, 3)


class FixtureTests(unittest.TestCase):
    def test_hundred_public_node_ids_and_nothing_else(self):
        raw = FIXTURE.read_text()
        doc = json.loads(raw)
        self.assertEqual(set(doc), {'about', 'source', 'node_ids'})
        self.assertEqual(len(doc['node_ids']), 100); self.assertEqual(len(set(doc['node_ids'])), 100)
        for node_id in doc['node_ids']:
            self.assertRegex(node_id, r'^(MDEwOlJlcG9zaXRvcnk[A-Za-z0-9+/=]+|R_kg[A-Za-z0-9_-]+)$')
        for forbidden in ('/Users', '/home/', 'token', 'ghp_', 'gho_', 'Bearer', '@'):
            self.assertNotIn(forbidden, raw)


def fake_fetch(ids, calls, drop=(), error_index=None, topics_more=(), cost=None, status=200):
    by_cost = cost or {'A': 1, 'B': 1, 'C': 2}

    def fetch(method, url, body, headers):
        calls.append({'method': method, 'url': url, 'headers': headers, 'body': json.loads(body)})
        if status != 200:
            return status, {}, b'{"message":"Bad credentials"}'
        q = json.loads(body)['query']
        kind = 'C' if 'repositoryTopics' in q else 'B' if 'description' in q else 'A'
        nodes = []
        for n, i in enumerate(json.loads(body)['variables']['ids']):
            if i in drop:
                nodes.append(None); continue
            node = {'id': i, 'nameWithOwner': f'o/r{n}', 'stargazerCount': 3000, 'createdAt': '2020-01-01T00:00:00Z', 'primaryLanguage': None}
            if kind != 'A':
                node['description'] = '' if n % 10 == 0 else f'repo {n}'
            if kind == 'C':
                if n % 7 == 0:
                    node['repositoryTopics'] = {'totalCount': 0, 'nodes': [], 'pageInfo': {'hasNextPage': False}}
                else:
                    more = i in topics_more
                    node['repositoryTopics'] = {'totalCount': 9 if more else 2, 'nodes': [{'topic': {'name': f't{k}'}} for k in range(5 if more else 2)],
                                                'pageInfo': {'hasNextPage': more}}
            nodes.append(node)
        answer = {'data': {'rateLimit': {'cost': by_cost[kind], 'limit': 1000, 'remaining': 900, 'resetAt': '2026-11-01T00:00:00Z', 'nodeCount': 100},
                           'nodes': nodes}}
        errors = [{'type': 'NOT_FOUND', 'path': ['nodes', ids.index(d)], 'message': f'Could not resolve to a node with the global id of {d!r}'} for d in drop]
        if error_index is not None and kind == 'C':
            nodes[error_index]['repositoryTopics'] = None
            errors.append({'type': 'FORBIDDEN', 'path': ['nodes', error_index, 'repositoryTopics'], 'message': 'field not accessible'})
        if errors:
            answer['errors'] = errors
        return 200, {'X-RateLimit-Limit': '1000', 'X-RateLimit-Remaining': '900', 'X-RateLimit-Resource': 'graphql'}, json.dumps(answer).encode()
    return fetch


class ProbeRunTests(unittest.TestCase):
    TOKEN = 'ghs_FAKE_TOKEN_should_never_appear'

    def run_probe(self, fetch):
        ids = json.loads(FIXTURE.read_text())['node_ids']
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict('os.environ', {'GH_TOKEN': self.TOKEN, 'GITHUB_ACTIONS': 'true'}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = probe.main(['--fixture', str(FIXTURE), '--out', tmp + '/o'], fetch=fetch(ids), sleeper=lambda s: None)
            files = {p.name: p.read_text() for p in pathlib.Path(tmp, 'o').iterdir()}
        return ids, code, out.getvalue(), files

    def test_three_sequential_queries_with_metrics_errors_and_topic_states(self):
        calls = []
        ids, code, stdout, files = self.run_probe(lambda ids: fake_fetch(ids, calls, drop={ids[3]}, error_index=5, topics_more={ids[1], ids[2]}))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c['method'] == 'POST' and c['url'] == 'https://api.github.com/graphql' for c in calls))
        self.assertTrue(all(c['body']['variables']['ids'] == ids for c in calls))
        self.assertEqual(set(files), {'metrics.json', 'metadata.json'})
        for text in [stdout, *files.values()]:
            self.assertNotIn(self.TOKEN, text); self.assertNotIn('Bearer', text); self.assertNotIn('Authorization', text)
        metrics = json.loads(files['metrics.json'])
        self.assertEqual(metrics['token_class'], 'actions-github-token')
        a, b, c = metrics['results']
        for r in (a, b, c):
            self.assertEqual(r['ids_asked'], 100)
            self.assertEqual(set(r['rate_limit']), {'cost', 'limit', 'remaining', 'resetAt', 'nodeCount'})
            self.assertGreater(r['response_bytes'], 0); self.assertGreater(r['request_bytes'], 0); self.assertIn('elapsed_ms', r)
            self.assertEqual(r['inaccessible_ids'], [ids[3]]); self.assertEqual(r['nodes_returned'], 99)
            self.assertEqual(len(r['attempts']), 1)
        self.assertEqual(c['errored_ids'], sorted({ids[3], ids[5]}))
        self.assertIn({'type': 'FORBIDDEN', 'path': ['nodes', 5, 'repositoryTopics'], 'message': 'field not accessible'}, c['errors'])
        self.assertEqual(b['description']['unknown'], 1); self.assertEqual(b['description']['empty'], 10)
        self.assertEqual(c['topics']['truncated'], 2); self.assertEqual(c['topics']['unknown'], 2)  # ids[3] null, ids[5] field error
        self.assertEqual(c['topics']['empty'] + c['topics']['complete'] + 4, 100)
        meta = json.loads(files['metadata.json'])
        self.assertEqual(meta[ids[1]]['topic_state'], 'truncated'); self.assertEqual(meta[ids[1]]['topics_total'], 9)
        self.assertEqual(meta[ids[3]]['topic_state'], 'unknown'); self.assertIsNone(meta[ids[3]]['full_name'])
        self.assertEqual(meta[ids[5]]['topic_state'], 'unknown'); self.assertEqual(meta[ids[0]]['topic_state'], 'empty')
        p = metrics['projection']
        self.assertEqual(p['C_description_topics']['projected_points'], 2 * 670); self.assertEqual(p['A_scalar']['projected_points'], 670)
        self.assertIn('separate cached incremental job', p['recommendation'])

    def test_a_failing_query_is_reported_with_bounded_retries_and_the_run_goes_red(self):
        calls = []
        ids, code, stdout, files = self.run_probe(lambda ids: fake_fetch(ids, calls, status=502))
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 3 * 3)  # three queries, at most three attempts each
        metrics = json.loads(files['metrics.json'])
        self.assertTrue(all(r['http_status'] == 502 and len(r['attempts']) == 3 for r in metrics['results']))
        self.assertIn('unmeasured', metrics['projection']['recommendation'])
        self.assertNotIn(self.TOKEN, files['metrics.json'] + stdout)


if __name__ == '__main__':
    unittest.main()
