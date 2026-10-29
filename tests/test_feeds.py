"""Gittrend feed semantics: network-free fixtures, provider isolation, honest coverage."""
import copy
import json
import http.client
from unittest import mock
import pathlib
import sqlite3
import tempfile
import unittest
from urllib.error import HTTPError

from trending.feeds import FeedClient, FeedStore, collect_feed


def repo(rid=1, name=None, stars=1000, gain=10):
    row = {'id': rid, 'fullName': name or f'owner/repo{rid}', 'stars': stars,
           'createdAt': '2020-01-01T00:00:00Z'}
    if gain is not None:
        row['starsToday'] = gain
    return row


def page(rows, number=1, total=None, pages=1, more=False, limit=100):
    return {'data': rows, 'pagination': {'page': number, 'limit': limit,
            'total': len(rows) if total is None else total, 'totalPages': pages, 'hasMore': more}}


class QueueFetch:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError('unexpected extra request')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def client(*responses, **kwargs):
    fetch = QueueFetch(*responses)
    return FeedClient(fetch=fetch, sleeper=lambda _: None, **kwargs), fetch


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def collect(self, rows, period='week'):
        c, _ = client(page(rows))
        return collect_feed(c, period)

    def store(self):
        s = FeedStore(self.root / 'feeds.sqlite3')
        return self.enterContext(s)

    def test_fetch_identifies_itself_and_asks_caches_to_revalidate(self):
        seen = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'{"ok": true}'
        with mock.patch('trending.feeds.urllib.request.urlopen', side_effect=lambda request, timeout: (seen.append(request), Response())[1]):
            self.assertEqual(FeedClient._fetch('https://gittrend.io/api/trending?time=day&page=1&limit=100'), {'ok': True})
        self.assertEqual(seen[0].get_header('Cache-control'), 'no-cache')
        self.assertEqual(seen[0].get_header('User-agent'), 'star-growth-dashboard (+https://github.com/neo-giga/star-growth-dashboard)')

    def test_pagination_fetches_every_page_with_consistent_coverage(self):
        c, f = client(page([repo(1), repo(2)], total=3, pages=2, more=True, limit=2),
                      page([repo(3)], number=2, total=3, pages=2, limit=2))
        result = collect_feed(c, 'week', limit=2)
        self.assertEqual(result['provider'], 'gittrend')
        self.assertEqual(result['period'], 'week')
        self.assertEqual(result['coverage']['status'], 'complete')
        self.assertEqual([r['repo_id'] for r in result['rows']], [1, 2, 3])
        self.assertEqual(len(f.urls), 2)

    def test_duplicate_stable_ids_dedup_but_never_claim_complete(self):
        c, _ = client(page([repo(1), repo(2)], total=4, pages=2, more=True, limit=2),
                      page([repo(2, name='renamed/project'), repo(3)], number=2, total=4, pages=2, limit=2))
        result = collect_feed(c, 'week', limit=2)
        self.assertEqual(result['coverage']['status'], 'inconsistent')
        self.assertEqual({r['repo_id'] for r in result['rows']}, {1, 2, 3})
        self.assertEqual(len(result['rows']), 3)

    def test_total_changes_during_pages_are_inconsistent(self):
        c, _ = client(page([repo(1), repo(2)], total=3, pages=2, more=True, limit=2),
                      page([repo(3), repo(4)], number=2, total=4, pages=2, limit=2))
        self.assertEqual(collect_feed(c, 'week', limit=2)['coverage']['status'], 'inconsistent')

    def test_page_budget_is_partial_not_complete(self):
        c, f = client(page([repo(1), repo(2)], total=5, pages=3, more=True, limit=2))
        result = collect_feed(c, 'week', limit=2, max_pages=1)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(len(f.urls), 1)

    def test_503_retry_succeeds_with_bounded_attempts(self):
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, None)
        c, f = client(error, page([repo()]), max_attempts=2)
        self.assertEqual(c.page('week', 1, 100)['data'][0]['id'], 1)
        self.assertEqual(len(f.urls), 2)

    def test_persistent_503_stops_at_bound(self):
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, None)
        c, f = client(error, error, error, max_attempts=2)
        with self.assertRaises((ValueError, RuntimeError, HTTPError)):
            c.page('week', 1, 100)
        self.assertEqual(len(f.urls), 2)

    def test_503_three_times_then_success_recovers_with_the_long_backoff(self):
        # 2026-10-28: month page 176 answered 503 three times within ~3 s and ended two live runs; it read fine later.
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, None)
        waits = []
        c = FeedClient(fetch=QueueFetch(error, error, error, page([repo()])), sleeper=waits.append)
        self.assertEqual(c.page('month', 176, 100)['data'][0]['id'], 1)
        self.assertEqual((waits, c.retries, c.requests), ([5, 15, 30], 3, 4))
        waits.clear()
        c = FeedClient(fetch=QueueFetch(*[error] * 6), sleeper=waits.append)
        with self.assertRaises(HTTPError):
            c.page('month', 176, 100)
        self.assertEqual(waits, [5, 15, 30, 60, 120])

    def test_retry_after_is_honoured_up_to_300_seconds(self):
        waits = []
        slow = HTTPError('https://example.invalid', 429, 'slow down', {'Retry-After': '240'}, None)
        c = FeedClient(fetch=QueueFetch(slow, page([repo()])), sleeper=waits.append)
        self.assertEqual(c.page('week', 1, 100)['data'][0]['id'], 1)
        self.assertEqual(waits, [240])
        too_slow = HTTPError('https://example.invalid', 429, 'slow down', {'Retry-After': '301'}, None)
        c = FeedClient(fetch=QueueFetch(too_slow), sleeper=waits.append)
        with self.assertRaisesRegex(ValueError, 'delay above 300 seconds'):
            c.page('week', 1, 100)

    def test_numeric_strings_and_missing_negative_gains(self):
        result = self.collect([repo(1, stars='10000', gain='9'), repo(2, stars='900', gain='100'),
                               repo(3, stars='500', gain=None), repo(4, stars='800', gain='-2')])
        rows = {r['repo_id']: r for r in result['rows']}
        self.assertEqual(rows[1]['stars'], 10000)
        self.assertEqual(rows[2]['gain'], 100)
        self.assertIsNone(rows[3]['gain'])
        self.assertEqual(rows[4]['gain'], -2)
        s = self.store()
        s.save(result)
        report = s.report(sort='7d')
        self.assertEqual([r['repo_id'] for r in report['ranked']], [2, 1, 4])
        self.assertEqual([r['repo_id'] for r in report['unknown_candidates']], [3])
        self.assertEqual([r['repo_id'] for r in s.report(sort='stars', period='week')['ranked']], [1, 2, 4, 3])

    def test_latest_refresh_rename_is_same_id_not_joined_duplicate(self):
        s = self.store()
        s.save(self.collect([repo(1, name='old/name', gain=10)]))
        s.save(self.collect([repo(1, name='new/name', gain=20)]))
        result = s.latest('week')
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['repo_id'], 1)
        self.assertEqual(result['rows'][0]['full_name'], 'new/name')
        self.assertEqual(s.report(sort='7d')['ranked'][0]['gain'], 20)

    def test_periods_remain_isolated_never_invent_other_window_gain(self):
        s = self.store()
        s.save(self.collect([repo(1, gain=111)], period='day'))
        s.save(self.collect([repo(1, gain=777)], period='week'))
        s.save(self.collect([repo(1, gain=3030)], period='month'))
        for sort, expected in [('1d', 111), ('7d', 777), ('30d', 3030)]:
            with self.subTest(sort=sort):
                report = s.report(sort=sort)
                self.assertEqual(report['ranked'][0]['gain'], expected)
        self.assertEqual(s.latest('week')['period'], 'week')

    def test_foreign_or_collector_database_rejected_without_mutation(self):
        for table in ('snapshots', 'gitpulse_totals', 'unrelated'):
            with self.subTest(table=table):
                p = self.root / (table + '.sqlite3')
                with sqlite3.connect(p) as c:
                    c.execute(f'CREATE TABLE {table}(id INTEGER)')
                before = p.read_bytes()
                with self.assertRaises((ValueError, RuntimeError)):
                    FeedStore(p)
                self.assertEqual(p.read_bytes(), before)

    def test_malformed_rows_never_become_fabricated_zero_or_complete(self):
        bad_rows = [dict(repo(), id=None), dict(repo(), stars=-1), dict(repo(), stars=True),
                    dict(repo(), stars='not-a-number'), dict(repo(), fullName='not-a-slug')]
        for row in bad_rows:
            with self.subTest(row=row):
                try:
                    result = self.collect([row])
                except (ValueError, RuntimeError):
                    continue
                self.assertNotEqual(result['coverage']['status'], 'complete')
                self.assertEqual(result['rows'], [])

    def test_source_asof_unknown_even_with_creation_date_and_fresh_cache(self):
        result = self.collect([repo()])
        self.assertIsNone(result['source_as_of'])
        self.assertEqual(result['source_as_of_status'], 'UNKNOWN')
        result['fetched_at'] = '2026-10-20T12:00:00+00:00'
        s = self.store()
        s.save(result)
        report = s.report(sort='7d', today='2026-10-26')
        self.assertTrue(report['cache_stale'])
        self.assertEqual(report['cache_stale_days'], 6)
        self.assertIsNone(report['source_as_of'])
        self.assertEqual(report['source_as_of_status'], 'UNKNOWN')
        fresh = s.report(sort='7d', today='2026-10-20')
        self.assertFalse(fresh['cache_stale'])
        self.assertIsNone(fresh['source_as_of'])

    def test_window_mismatch_is_refused_not_substituted(self):
        s = self.store()
        s.save(self.collect([repo()], period='week'))
        with self.assertRaises(ValueError):
            s.report(sort='30d', period='week')
        with self.assertRaises(ValueError):
            s.report(sort='30d')

    def test_terminal_error_preserves_earlier_rows_as_partial(self):
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, None)
        c, f = client(page([repo(1)], total=2, pages=2, more=True, limit=1), error, max_attempts=1)
        result = collect_feed(c, 'week', limit=1)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual([r['repo_id'] for r in result['rows']], [1])
        self.assertEqual(len(f.urls), 2)
        self.assertTrue(result['coverage']['warnings'])
        s = self.store()
        s.save(result)
        self.assertEqual(s.report(sort='7d')['coverage']['status'], 'partial')
        self.assertIn('Retrieved rows', s.report(sort='7d')['ranking_scope'])

    def test_foreign_provider_save_is_rejected_without_overwriting_latest(self):
        s = self.store()
        result = self.collect([repo(1)])
        s.save(result)
        foreign = copy.deepcopy(result)
        foreign['provider'] = 'gitpulse'
        foreign['rows'][0]['gain'] = 999
        with self.assertRaises(ValueError):
            s.save(foreign)
        self.assertEqual(s.latest('week')['rows'][0]['gain'], 10)

    def test_incomplete_read_retries_then_success_or_bounded_partial(self):
        err = http.client.IncompleteRead(b'partial', 100)
        c, f = client(err, page([repo()]), max_attempts=2)
        self.assertEqual(collect_feed(c, 'week')['coverage']['status'], 'complete')
        self.assertEqual(len(f.urls), 2)
        c, f = client(err, err, err, max_attempts=2)
        result = collect_feed(c, 'week')
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(len(f.urls), 2)
        self.assertEqual(result['rows'], [])

    def test_conflicting_casefolded_slug_ids_are_not_merged_or_complete(self):
        result = self.collect([repo(1, name='Owner/Project'), repo(2, name='owner/project')])
        self.assertEqual(result['coverage']['status'], 'inconsistent')
        self.assertEqual({r['repo_id'] for r in result['rows']}, {1, 2})
        self.assertTrue(any('conflicting' in w for w in result['coverage']['warnings']))

    def test_huge_provider_id_is_partial_not_sqlite_overflow(self):
        result = self.collect([repo(2**100)])
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(result['rows'], [])
        self.store().save(result)

    def test_last_complete_feed_survives_failed_refresh_with_warning(self):
        s = self.store()
        complete = self.collect([repo(1, gain=77)])
        complete['fetched_at'] = '2026-10-20T10:00:00+00:00'
        good_id = s.save(complete)
        c, _ = client(HTTPError('https://example.invalid', 503, 'down', {}, None), max_attempts=1)
        partial = collect_feed(c, 'week')
        latest_id = s.save(partial)
        report = s.report(sort='7d', today='2026-10-26')
        self.assertEqual(report['run_id'], good_id)
        self.assertEqual(report['latest_attempt']['run_id'], latest_id)
        self.assertEqual(report['latest_attempt']['coverage']['status'], 'partial')
        self.assertTrue(report['refresh_warning'])
        self.assertEqual(report['fetched_at'], complete['fetched_at'])
        self.assertEqual(report['ranked'][0]['gain'], 77)
        self.assertEqual(report['cache_stale_days'], 6)

    def test_checkpoint_resume_retains_payload_timestamp_and_request_span(self):
        cache = self.root / 'checkpoint'
        timestamp = '2026-10-20T12:34:56+00:00'
        fetch = QueueFetch(page([repo()]))
        c = FeedClient(fetch=fetch, cache_dir=cache)
        with mock.patch('trending.feeds.utcnow', return_value=timestamp):
            first = c.page('week', 1, 100)
        self.assertEqual(c.last_fetched_at, timestamp)
        saved = list(cache.glob('*.json'))
        self.assertEqual(len(saved), 1)
        envelope = json.loads(saved[0].read_text())
        self.assertEqual(envelope['payload'], first)
        self.assertEqual(envelope['fetched_at'], timestamp)
        forbidden = mock.Mock(side_effect=AssertionError('resume must not fetch'))
        resumed = FeedClient(fetch=forbidden, cache_dir=cache, resume=True)
        result = collect_feed(resumed, 'week')
        forbidden.assert_not_called()
        self.assertEqual(resumed.cached_pages, 1)
        self.assertEqual(resumed.last_fetched_at, timestamp)
        self.assertEqual(result['fetched_started_at'], timestamp)
        self.assertEqual(result['fetched_at'], timestamp)
        self.assertEqual(result['cached_pages_reused'], 1)
        self.assertEqual(result['coverage']['status'], 'complete')

    def test_checkpoint_refuses_nonresume_and_wrong_provider_or_url(self):
        cache = self.root / 'checkpoint'
        original = FeedClient(fetch=QueueFetch(page([repo()])), cache_dir=cache)
        original.page('week', 1, 100)
        p = next(cache.glob('*.json'))
        saved = json.loads(p.read_text())
        forbidden = mock.Mock(side_effect=AssertionError('must not fetch'))
        with self.assertRaises(ValueError):
            FeedClient(fetch=forbidden, cache_dir=cache).page('week', 1, 100)
        for key, value in [('provider', 'foreign'), ('url', 'https://evil.invalid/api')]:
            with self.subTest(key=key):
                p.write_text(json.dumps({**saved, key: value}))
                with self.assertRaises(ValueError):
                    FeedClient(fetch=forbidden, cache_dir=cache, resume=True).page('week', 1, 100)
        forbidden.assert_not_called()

    def test_missing_gain_stays_unknown_not_zero(self):
        s = self.store()
        s.save(self.collect([repo(gain=None)]))
        result = s.report(sort='7d')
        self.assertEqual(result['ranked'], [])
        self.assertEqual(len(result['unknown_candidates']), 1)
        self.assertIsNone(result['unknown_candidates'][0]['gain'])

    def test_malformed_checkpoint_shapes_are_partial_not_uncaught(self):
        cache = self.root / 'checkpoint-shapes'
        FeedClient(fetch=QueueFetch(page([repo()])), cache_dir=cache).page('week', 1, 100)
        path = next(cache.glob('*.json'))
        original = json.loads(path.read_text())
        variants = [[], None, {}, {**original, 'payload': []}, {**original, 'fetched_at': None},
                    {k: v for k, v in original.items() if k != 'payload'},
                    {k: v for k, v in original.items() if k != 'fetched_at'}]
        forbidden = mock.Mock(side_effect=AssertionError('no network'))
        for bad in variants:
            with self.subTest(shape=bad):
                path.write_text(json.dumps(bad))
                r = collect_feed(FeedClient(fetch=forbidden, cache_dir=cache, resume=True), 'week')
                self.assertEqual(r['coverage']['status'], 'partial')
                self.assertTrue(r['coverage']['failure'])
        forbidden.assert_not_called()

    def test_checkpoint_span_orders_absolute_times_not_iso_strings(self):
        cache = self.root / 'offset-checkpoint'
        c = FeedClient(fetch=QueueFetch(page([repo(1)], total=2, pages=2, more=True, limit=1),
                                       page([repo(2)], number=2, total=2, pages=2, limit=1)), cache_dir=cache)
        c.page('week', 1, 1)
        c.page('week', 2, 1)
        times = ['2026-10-20T12:00:00+02:00', '2026-10-20T11:00:00+00:00']
        for path, stamp in zip(sorted(cache.glob('*.json')), times):
            envelope = json.loads(path.read_text()); envelope['fetched_at'] = stamp
            path.write_text(json.dumps(envelope))
        r = collect_feed(FeedClient(fetch=mock.Mock(side_effect=AssertionError('no network')), cache_dir=cache, resume=True), 'week', limit=1)
        self.assertEqual(r['fetched_started_at'], times[0])
        self.assertEqual(r['fetched_at'], times[1])

    def test_requeued_last_page_that_reads_fine_reaches_the_end(self):
        # Bugbot on full-lists r2: a re-queued last page's successful read ignored hasMore, so the window stayed partial.
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, None)
        c, f = client(page([repo(1)], total=2, pages=2, more=True, limit=1), error, page([repo(2)], number=2, total=2, pages=2, limit=1), max_attempts=1)
        result = collect_feed(c, 'week', limit=1, max_pages=2, requeue=True)
        self.assertEqual((result['coverage']['status'], result['coverage']['reached_end'], result['coverage']['failure'], result['coverage']['requeued_pages']), ('complete', True, None, [2]))
        self.assertEqual([r['repo_id'] for r in result['rows']], [1, 2])
        self.assertEqual([u.split('page=')[1][0] for u in f.urls], ['1', '2', '2'])

    def test_failed_last_page_is_requeued_without_reading_past_the_end(self):
        # The last page fails once and every page past it always fails: pages after the provider's last page are never read, so
        # their failures cannot trip the three-in-a-row stop.
        pages = {1: page([repo(1)], total=2, pages=2, more=True, limit=1), 2: page([repo(2)], number=2, total=2, pages=2, limit=1)}
        reads = []
        def fetch(url):
            number = int(url.split('page=')[1].split('&')[0]); reads.append(number)
            if number > 2 or reads.count(2) == 1:
                raise HTTPError(url, 503, 'unavailable', {}, None)
            return copy.deepcopy(pages[number])
        result = collect_feed(FeedClient(fetch=fetch, sleeper=lambda _: None, max_attempts=1), 'week', limit=1, requeue=True)
        self.assertEqual((result['coverage']['status'], result['coverage']['reached_end'], result['coverage']['failure'], result['coverage']['requeued_pages']), ('complete', True, None, [2]))
        self.assertEqual(reads, [1, 2, 2])

    def test_requeued_last_page_failing_again_still_fails_the_window(self):
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, None)
        c, f = client(page([repo(1)], total=2, pages=2, more=True, limit=1), error, error, error, error, max_attempts=1)
        result = collect_feed(c, 'week', limit=1, requeue=True)
        self.assertEqual((result['coverage']['status'], result['coverage']['reached_end']), ('partial', False))
        self.assertRegex(result['coverage']['failure'], '^page 2 failed again after re-queue: HTTPError')
        self.assertEqual([u.split('page=')[1][0] for u in f.urls], ['1', '2', '2'])

    def test_requeued_pages_past_a_shrunken_end_are_not_reread(self):
        # The total shrank while pages 2 and 3 failed: page 2's re-read reports the end, so page 3 is beyond it and is not read.
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, None)
        c, f = client(page([repo(1)], total=3, pages=3, more=True, limit=1), error, error, page([repo(2)], number=2, total=2, pages=2, limit=1), max_attempts=1)
        result = collect_feed(c, 'week', limit=1, max_pages=3, requeue=True)
        self.assertEqual((result['coverage']['reached_end'], result['coverage']['failure'], result['coverage']['requeued_pages']), (True, None, [2, 3]))
        self.assertEqual([u.split('page=')[1][0] for u in f.urls], ['1', '2', '3', '2'])


if __name__ == '__main__':
    unittest.main()
