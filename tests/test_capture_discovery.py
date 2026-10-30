"""Discovery: star-range slices, the 1,000-result cap, reuse of yesterday's plan, failures and deadlines recorded, never fatal."""
import pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture.discovery import discover, initial_plan, next_plan, query, total_count
from capture.github import GitHub
from fake_github import FakeClock, make_world


def client(world, **kw):
    clock = FakeClock()
    return GitHub('t', fetch=world.fetch, sleeper=clock.sleep, clock=clock, **{'max_attempts': 3, **kw}), clock


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.world = make_world(repos=3000, seed=7)
        self.above = {i for i in self.world.repos if self.world.stars(i, self.world.now) > 2000}

    def test_lists_the_whole_set_from_scratch_in_slices_under_the_cap(self):
        gh, clock = client(self.world)
        result = discover(gh, 2000)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(set(result['repos']), self.above)
        self.assertTrue(all(s['total'] <= 800 for s in result['slices'] if s['status'] == 'ok'))
        self.assertTrue(all(s['got'] == s['total'] for s in result['slices'] if s['status'] == 'ok'))
        self.assertIsNone(result['slices'][-1]['hi']); self.assertEqual(result['slices'][0]['lo'], 2001)
        plan = result['next_plan']
        self.assertEqual(plan[0]['lo'], 2001); self.assertIsNone(plan[-1]['hi'])
        self.assertTrue(all(plan[i]['hi'] + 1 == plan[i + 1]['lo'] for i in range(len(plan) - 1)))
        searches = [u for _, u, _ in self.world.requests if 'search' in u]
        self.assertLessEqual(len(searches), 2 * sum((s['total'] + 99) // 100 for s in result['slices'] if s['status'] == 'ok') + 10)
        item = next(iter(result['repos'].values()))
        self.assertEqual(set(item), {'full_name', 'stars', 'language', 'created_at'})

    def test_yesterdays_plan_lists_without_probing_and_a_grown_slice_is_split(self):
        gh, _ = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        self.world.requests.clear()
        gh, _ = client(self.world)
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(set(result['repos']), self.above)
        self.assertEqual(len(self.world.requests), sum((s['total'] + 99) // 100 for s in result['slices'] if s['status'] == 'ok'))
        wide = [{'lo': 2001, 'hi': None}]  # everything in one slice: over the cap, split until each half fits
        result = discover(gh, 2000, slices=wide)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(set(result['repos']), self.above)
        self.assertTrue(any(s['status'] == 'split' for s in result['slices']))
        self.assertTrue(all(s['total'] <= 800 for s in result['slices'] if s['status'] == 'ok'))

    def test_a_higher_threshold_or_a_stale_plan_start_is_corrected(self):
        gh, _ = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        result = discover(gh, 5000, slices=plan)
        self.assertEqual(set(result['repos']), {i for i in self.world.repos if self.world.stars(i, self.world.now) > 5000})
        self.assertEqual(result['slices'][0]['lo'], 5001)

    def test_search_down_is_a_failed_discovery_not_an_exception(self):
        self.world.search_down = True
        gh, _ = client(self.world)
        self.assertIsNone(total_count(gh, 2000))
        result = discover(gh, 2000, slices=[{'lo': 2001, 'hi': 2100}, {'lo': 2101, 'hi': None}])
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['repos'], {})
        self.assertEqual([s['status'] for s in result['slices']], ['failed', 'failed'])
        self.assertEqual(result['next_plan'], [{'lo': 2001, 'hi': 2100}, {'lo': 2101, 'hi': None}])

    def test_a_flaky_page_is_retried_and_a_dead_slice_is_recorded_as_partial_with_the_rest_kept(self):
        gh, _ = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        self.world.flaky['&page=2&'] = 1  # the first attempt at a page 2 answers 502; the retry succeeds at the same page size
        gh, _ = client(self.world)
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(set(result['repos']), self.above)
        self.world.flaky['per_page=30&page=3&'] = 10 ** 6; self.world.max_per_page = 30  # page 3 dead at the smallest size
        gh, _ = client(self.world)
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'partial')
        partial = [s for s in result['slices'] if s['status'] == 'partial']
        self.assertTrue(partial); self.assertTrue(all(s['page_size'] == 30 and s['pages'] < -(-s['total'] // 30) and s['got'] < s['total'] for s in partial))
        self.assertLess(len(result['repos']), len(self.above)); self.assertTrue(set(result['repos']) <= self.above)
        self.assertEqual(result['slices_failed'], len(partial)); self.assertEqual(result['slices_ok'] + len(partial), len(result['slices']))

    def test_a_later_page_failing_retries_at_the_slice_size_instead_of_relisting(self):
        gh, _ = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        self.world.flaky['per_page=100&page=3&'] = 3  # three 502s on a later page: retried at 100, never a size drop
        self.world.requests.clear()
        gh, clock = client(self.world, max_attempts=8)
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(result['page_size'], 100); self.assertEqual(set(result['repos']), self.above)
        self.assertEqual(len(self.world.requests), sum((s['total'] + 99) // 100 for s in result['slices'] if s['status'] == 'ok') + 3)

    def test_a_crowded_top_end_is_halved_until_it_fits_and_the_halves_keep_their_bounds(self):
        # 2026-10-29: 1,949 real repos had >= 22,854 stars; the open-ended slice must split into bounded halves that converge
        world = make_world(repos=200, seed=3)
        for n in range(1200):
            world.add(f'R_top{n:04d}', f'top/repo{n}', [20 + n] * 100 + [0] * 700)  # 20,000..21,999 stars after 100 days... scaled below
        for n in range(1200):
            repo = world.repos[f'R_top{n:04d}']; repo['arrivals'] = [0] * 700 + [(20000 + 50 * n) // 100] * 100
            repo['prefix'] = [0]
            for x in repo['arrivals']:
                repo['prefix'].append(repo['prefix'][-1] + x)
        gh, _ = client(world)
        above = {i for i in world.repos if world.stars(i, world.now) > 2000}
        result = discover(gh, 2000)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(set(result['repos']), above)
        splits = [s for s in result['slices'] if s['status'] == 'split']
        self.assertGreater(len(splits), 0); self.assertLess(len(splits), 12)
        self.assertEqual(len({(s['lo'], s['hi']) for s in splits}), len(splits))  # no slice split twice
        self.assertIsNone(result['next_plan'][-1]['hi'])

    def test_a_degraded_search_drops_the_page_size_and_still_lists_everything(self):
        gh, clock = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        self.world.max_per_page = 50
        self.world.requests.clear()
        gh, clock = client(self.world)
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(set(result['repos']), self.above); self.assertEqual(result['page_size'], 50)
        sizes = [u.split('per_page=')[1].split('&')[0] for _, u, _ in self.world.requests]
        self.assertEqual(sizes.count('100'), 2)  # two quick attempts, then 50 for the rest of the run
        self.assertTrue(all(s['page_size'] == 50 for s in result['slices'] if s['status'] == 'ok'))
        self.world.max_per_page = 29  # nothing works: failed, not an exception
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['page_size'], 30)

    def test_the_deadline_stops_retry_waits_inside_the_client(self):
        self.world.search_down = True
        gh, clock = client(self.world, max_attempts=8)
        result = discover(gh, 2000, slices=[{'lo': 2001, 'hi': 2100}, {'lo': 2101, 'hi': None}], deadline_seconds=30, clock=clock)
        self.assertEqual(result['status'], 'failed'); self.assertLess(sum(clock.sleeps), 60); self.assertIsNone(gh.deadline)
        self.assertIn('deadline', result['slices'][0]['error'])

    def test_a_surprise_exception_in_one_slice_keeps_the_repos_already_found(self):
        gh, _ = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        real_fetch = self.world.fetch

        def fetch(method, url, body, headers):
            if 'stars%3A2001..' in url:  # the first slice is poison; the 2026-10-29 run lost a whole listing to one IncompleteRead
                raise RuntimeError('boom')
            return real_fetch(method, url, body, headers)
        gh, _ = client(self.world)
        gh.fetch = fetch
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'partial'); self.assertEqual(result['slices'][0]['status'], 'failed'); self.assertIn('boom', result['slices'][0]['error'])
        self.assertGreater(len(result['repos']), len(self.above) // 2); self.assertIsNone(gh.deadline)

    def test_a_deadline_skips_the_remaining_slices_and_max_slices_bounds_a_dry_run(self):
        gh, clock = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        clock.tick = 1.0
        result = discover(gh, 2000, slices=plan, deadline_seconds=3, clock=clock)
        self.assertEqual(result['status'], 'partial'); self.assertGreater(result['slices_skipped'], 0)
        self.assertEqual(next_plan(result['slices'], 2000), plan)  # skipped slices keep their boundaries for tomorrow
        gh, _ = client(self.world)
        result = discover(gh, 2000, slices=plan, max_slices=2)
        self.assertEqual(result['slices_ok'], 2); self.assertEqual(result['slices_skipped'], len(plan) - 2)
        self.world.max_per_page = 30  # a page-size drop re-lists the slice; that is not a second slice (the 2026-10-29 dry run listed nothing)
        gh, _ = client(self.world)
        result = discover(gh, 2000, slices=plan, max_slices=1)
        self.assertEqual(result['slices_ok'], 1); self.assertEqual(result['page_size'], 30); self.assertEqual(result['slices_skipped'], len(plan) - 1)

    def test_total_count_and_query_and_plan_helpers(self):
        gh, _ = client(self.world)
        self.assertEqual(total_count(gh, 2000), len(self.above))
        self.assertEqual(query(2001, 2030), 'stars:2001..2030 fork:true'); self.assertEqual(query(50000, None), 'stars:>=50000 fork:true')
        self.assertEqual(initial_plan(2000), [{'lo': 2001, 'hi': 2030}])
        slices = [{'lo': 2001, 'hi': 2100, 'total': 200, 'status': 'ok'}, {'lo': 2101, 'hi': 2300, 'total': 300, 'status': 'ok'},
                  {'lo': 2301, 'hi': 2400, 'total': 500, 'status': 'ok'}, {'lo': 2401, 'hi': 2600, 'total': 900, 'status': 'split'},
                  {'lo': 2401, 'hi': 2500, 'total': 450, 'status': 'ok'}, {'lo': 2501, 'hi': 2600, 'total': 450, 'status': 'ok'}]
        self.assertEqual(next_plan(slices, 2000), [{'lo': 2001, 'hi': 2300}, {'lo': 2301, 'hi': 2400}, {'lo': 2401, 'hi': 2500}, {'lo': 2501, 'hi': 2600}, {'lo': 2601, 'hi': None}])

    def test_forks_above_the_threshold_are_listed_and_counted_in_the_total(self):
        forks = sorted(self.above)[::50]
        for node_id in forks:
            self.world.repos[node_id]['fork'] = True  # search leaves these out unless the query says fork:true
        gh, _ = client(self.world)
        self.assertEqual(total_count(gh, 2000), len(self.above))
        result = discover(gh, 2000)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(set(result['repos']), self.above); self.assertTrue(set(forks) <= set(result['repos']))

    def test_incomplete_results_make_the_slice_partial_and_the_total_untrusted(self):
        gh, _ = client(self.world)
        plan = discover(gh, 2000)['next_plan']
        self.world.incomplete.add('page=2&')  # every page 2 timed out on GitHub's side but answered 200
        gh, _ = client(self.world)
        result = discover(gh, 2000, slices=plan)
        self.assertEqual(result['status'], 'partial')
        partial = [s for s in result['slices'] if s['status'] == 'partial']
        self.assertTrue(partial); self.assertTrue(all('incomplete_results' in s['error'] and s['pages'] > 1 for s in partial))
        self.assertEqual(set(result['repos']), self.above)  # the items are kept
        self.world.incomplete = {'per_page=1&'}  # the total probe
        self.assertIsNone(total_count(gh, 2000))

if __name__ == '__main__':
    unittest.main()
