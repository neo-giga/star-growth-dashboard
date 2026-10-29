"""The capture's GitHub client: retries, rate-limit waits, search pacing, the sliding-hour core budget, the request log."""
import json, unittest
from capture.github import GitHub, GitHubError
import pathlib, sys; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fake_github import FakeClock


class ScriptedFetch:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, extra, payload = answer
        return status, extra, (json.dumps(payload).encode() if not isinstance(payload, bytes) else payload)


def client(fetch, **kw):
    clock = FakeClock()
    return GitHub('tok', fetch=fetch, sleeper=clock.sleep, clock=clock, wall=lambda: 1_700_000_000 + clock.t, **kw), clock


class GitHubClientTests(unittest.TestCase):
    def test_transient_answers_are_retried_with_growing_waits_and_the_token_is_sent(self):
        fetch = ScriptedFetch((502, {}, b'bad'), (504, {}, b'bad'), (200, {'X-RateLimit-Resource': 'core'}, {'ok': 1}))
        gh, clock = client(fetch)
        data, headers = gh.rest('/repos/a/b')
        self.assertEqual(data, {'ok': 1}); self.assertEqual(headers['x-ratelimit-resource'], 'core')
        self.assertEqual(clock.sleeps, [2, 5]); self.assertEqual(gh.counts['retries'], 2)
        self.assertEqual(fetch.calls[0][3]['Authorization'], 'Bearer tok'); self.assertEqual(fetch.calls[0][1], 'https://api.github.com/repos/a/b')

    def test_connection_errors_and_truncated_bodies_count_as_transient(self):
        import http.client
        fetch = ScriptedFetch(ConnectionResetError('reset'), TimeoutError('slow'), http.client.IncompleteRead(b'', 163783), (200, {}, [1]))
        gh, clock = client(fetch)
        self.assertEqual(gh.rest('/x')[0], [1]); self.assertEqual(clock.sleeps, [2, 5, 10])

    def test_the_deadline_cuts_a_retry_wait_short(self):
        gh, clock = client(ScriptedFetch((502, {}, b''), (502, {}, b''), (200, {}, {})))
        gh.deadline = clock.t + 4
        with self.assertRaises(GitHubError) as caught:
            gh.rest('/x')
        self.assertEqual(caught.exception.status, 'deadline'); self.assertEqual(clock.sleeps, [2])  # the 5 s wait would pass it

    def test_client_errors_raise_at_once_404_included(self):
        for status in (404, 422, 401):
            gh, clock = client(ScriptedFetch((status, {}, {'message': 'no'})))
            with self.assertRaises(GitHubError) as caught:
                gh.rest('/x')
            self.assertEqual(caught.exception.status, status); self.assertEqual(clock.sleeps, [])

    def test_gives_up_after_max_attempts(self):
        gh, clock = client(ScriptedFetch(*[(502, {}, b'x')] * 3), max_attempts=3)
        with self.assertRaises(GitHubError):
            gh.rest('/x')
        self.assertEqual(len(clock.sleeps), 2); self.assertEqual(gh.counts['failures'], 1)

    def test_a_primary_rate_limit_waits_for_the_reset_and_a_secondary_one_for_retry_after(self):
        gh, clock = client(ScriptedFetch((403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(1_700_000_000 + 1000 + 300)}, {'message': 'API rate limit exceeded'}),
                                         (200, {}, {})))
        gh.rest('/x'); self.assertEqual(clock.sleeps, [302.0])
        gh, clock = client(ScriptedFetch((429, {'retry-after': '7'}, {'message': 'secondary rate limit'}), (200, {}, {})))
        gh.rest('/x'); self.assertEqual(clock.sleeps, [7.0])
        gh, clock = client(ScriptedFetch((403, {}, {'message': 'You have exceeded a secondary rate limit'}), (200, {}, {})))
        gh.rest('/x'); self.assertEqual(clock.sleeps, [60])

    def test_a_403_that_is_not_a_rate_limit_is_final(self):
        gh, clock = client(ScriptedFetch((403, {'x-ratelimit-remaining': '4000'}, {'message': 'Resource not accessible by integration'})))
        with self.assertRaises(GitHubError):
            gh.rest('/x')
        self.assertEqual(clock.sleeps, [])

    def test_search_is_paced_to_the_per_minute_limit(self):
        gh, clock = client(ScriptedFetch(*[(200, {}, {'total_count': 0, 'items': []})] * 4), search_per_minute=30)
        for _ in range(4):
            gh.search_repositories('stars:>1', page=1)
        self.assertEqual(clock.sleeps, [2.0, 2.0, 2.0])
        gh, clock = client(ScriptedFetch(*[(200, {}, {'total_count': 0, 'items': []})] * 2))  # the default keeps under the limit
        gh.search_repositories('x'); gh.search_repositories('x')
        self.assertAlmostEqual(clock.sleeps[0], 60 / 28)

    def test_search_retries_wait_briefly(self):
        gh, clock = client(ScriptedFetch((502, {}, b''), (504, {}, b''), (502, {}, b''), (200, {}, {'total_count': 0, 'items': []})), search_per_minute=30)
        gh.search_repositories('stars:>1')
        self.assertEqual(clock.sleeps, [2, 3, 5])

    def test_search_query_parameters(self):
        fetch = ScriptedFetch((200, {}, {'total_count': 0, 'items': []}))
        gh, _ = client(fetch)
        gh.search_repositories('stars:2001..2030', page=3)
        self.assertEqual(fetch.calls[0][1], 'https://api.github.com/search/repositories?q=stars%3A2001..2030&per_page=100&page=3&sort=stars&order=asc')

    def test_the_sliding_hour_core_budget_is_never_exceeded(self):
        fetch = ScriptedFetch(*[(200, {}, {})] * 7)
        gh, clock = client(fetch, core_per_hour=3)
        for _ in range(7):
            gh.rest('/x')
        # requests 1-3 at t=1000, the 4th waits an hour (then 4-6 go at once), the 7th waits another hour
        self.assertEqual(len(clock.sleeps), 2)
        self.assertTrue(all(abs(s - 3600.01) < 0.02 for s in clock.sleeps), clock.sleeps)
        # search and graphql requests are not core
        gh2, clock2 = client(ScriptedFetch(*[(200, {}, {'total_count': 0, 'items': []})] * 2), core_per_hour=1, search_per_minute=30)
        gh2.search_repositories('x'); gh2.search_repositories('x')
        self.assertEqual(clock2.sleeps, [2.0])

    def test_the_request_log_records_every_attempt(self):
        log = []
        gh, clock = client(ScriptedFetch((502, {}, b''), (200, {'X-RateLimit-Resource': 'core', 'X-RateLimit-Remaining': '4'}, {})), request_log=log.append)
        gh.rest('/repos/a/b')
        self.assertEqual([e['status'] for e in log], [502, 200]); self.assertEqual(log[1]['remaining'], '4'); self.assertEqual(log[1]['resource'], 'core')
        self.assertTrue(log[0]['at'].endswith('+00:00')); self.assertEqual(log[1]['url'], 'https://api.github.com/repos/a/b')

    def test_graphql_returns_partial_data_and_retries_rate_limited_and_raises_on_errors_only(self):
        fetch = ScriptedFetch((200, {}, {'data': {'nodes': [None, {'id': 'x'}]}, 'errors': [{'type': 'NOT_FOUND'}]}))
        gh, _ = client(fetch)
        self.assertEqual(gh.graphql('q', {'ids': ['a', 'x']})['nodes'], [None, {'id': 'x'}])
        self.assertEqual(json.loads(fetch.calls[0][2]), {'query': 'q', 'variables': {'ids': ['a', 'x']}}); self.assertEqual(fetch.calls[0][0], 'POST')
        gh, clock = client(ScriptedFetch((200, {}, {'data': None, 'errors': [{'type': 'RATE_LIMITED'}]}), (200, {}, {'data': {'ok': 1}})))
        self.assertEqual(gh.graphql('q'), {'ok': 1}); self.assertEqual(clock.sleeps, [60])
        gh, _ = client(ScriptedFetch((200, {}, {'errors': [{'message': 'syntax'}]})))
        with self.assertRaises(GitHubError):
            gh.graphql('q')
        gh, clock = client(ScriptedFetch((502, {}, b'<html>'), (200, {}, {'data': {'ok': 2}})))
        self.assertEqual(gh.graphql('q'), {'ok': 2}); self.assertEqual(clock.sleeps, [2])


if __name__ == '__main__':
    unittest.main()
