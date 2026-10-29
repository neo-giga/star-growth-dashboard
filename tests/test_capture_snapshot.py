"""The GraphQL snapshot: 100 IDs per query, concurrent workers, missing nodes and failed batches counted, no search."""
import pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture.github import GitHub
from capture.snapshot import QUERY, read_counts
from fake_github import FakeClock, make_world


class SnapshotTests(unittest.TestCase):
    def test_reads_every_repo_in_batches_of_100_with_the_metadata_renames_need(self):
        world = make_world(repos=250, seed=2)
        clock = FakeClock()
        gh = GitHub('t', fetch=world.fetch, sleeper=clock.sleep, clock=clock)
        for workers in (1, 4):
            world.requests.clear()
            rows, stats = read_counts(gh, list(world.repos) + ['R_gone'], workers=workers)
            self.assertEqual(stats['queries'], 3); self.assertEqual(stats['points'], 3); self.assertEqual(stats['read'], 250); self.assertEqual(stats['missing'], 1)
            self.assertEqual(stats['failed_queries'], 0); self.assertEqual(len(world.requests), 3)
            self.assertTrue(all(u.endswith('/graphql') for _, u, _ in world.requests))
            for node_id, row in rows.items():
                self.assertEqual(row['stars'], world.stars(node_id, world.now)); self.assertEqual(row['full_name'], world.repos[node_id]['full_name'])
                self.assertEqual(row['language'], world.repos[node_id]['language']); self.assertEqual(row['created_at'], world.repos[node_id]['created_at'])
        self.assertIn('nodes(ids: $ids)', QUERY); self.assertIn('stargazerCount', QUERY); self.assertNotIn('search', QUERY)

    def test_a_batch_that_fails_after_retries_leaves_the_others_read(self):
        world = make_world(repos=250, seed=2)
        clock = FakeClock()
        gh = GitHub('t', fetch=world.fetch, sleeper=clock.sleep, clock=clock, max_attempts=2)
        world.flaky['graphql'] = 1  # one 502, then fine: retried within the batch
        rows, stats = read_counts(gh, list(world.repos), workers=1)
        self.assertEqual((stats['read'], stats['failed_queries']), (250, 0)); self.assertEqual(clock.sleeps, [2])
        world.flaky['graphql'] = 2  # two 502s exceed max_attempts=2: that batch is lost today
        rows, stats = read_counts(gh, list(world.repos), workers=1)
        self.assertEqual((stats['read'], stats['failed_queries'], stats['queries']), (150, 1, 3)); self.assertEqual(len(rows), 150)

    def test_an_empty_set_costs_nothing(self):
        world = make_world(repos=1)
        gh = GitHub('t', fetch=world.fetch)
        self.assertEqual(read_counts(gh, [])[1]['queries'], 0); self.assertEqual(world.requests, [])


if __name__ == '__main__':
    unittest.main()
