"""Folding GitHub's weekly history pages into per-day records, Pacific days, and summing a date range."""
import datetime as dt, pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capture.history import WINDOWS, fold, pacific_date, read_pages, stars_between
from fake_github import FakeClock, World, make_world
from capture.github import GitHub

UTC = dt.timezone.utc


def week(sunday, days):
    return {'week': int(dt.datetime(sunday.year, sunday.month, sunday.day, tzinfo=UTC).timestamp()), 'total': sum(days), 'days': days}


class PacificDateTests(unittest.TestCase):
    def test_pacific_date_is_the_calendar_day_in_los_angeles(self):
        self.assertEqual(pacific_date(dt.datetime(2026, 10, 30, 5, 30, tzinfo=UTC)), dt.date(2026, 10, 29))  # 22:30 PDT the day before
        self.assertEqual(pacific_date(dt.datetime(2026, 10, 30, 7, 0, tzinfo=UTC)), dt.date(2026, 10, 30))  # 00:00 PDT
        self.assertEqual(pacific_date(dt.datetime(2026, 12, 1, 7, 30, tzinfo=UTC)), dt.date(2026, 11, 30))  # 23:30 PST
        self.assertEqual(WINDOWS, (1, 7, 15, 30, 90, 180, 365))


class FoldTests(unittest.TestCase):
    def test_days_run_oldest_first_and_stop_at_the_anchor_day(self):
        s1, s2 = dt.date(2026, 10, 18), dt.date(2026, 10, 25)
        weeks = [week(s2, [10, 11, 12, 13, 14, 0, 0]), week(s1, [1, 2, 3, 4, 5, 6, 7])]  # newest first, Thursday the 29th in progress
        record = fold(weeks, dt.datetime(2026, 10, 29, 15, 0, tzinfo=UTC), 5000, pages=1, has_more=False)
        self.assertEqual(record['start'], '2026-10-18'); self.assertEqual(record['through'], '2026-10-29')
        self.assertEqual(record['days'], [1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 14]); self.assertEqual(record['anchor_stars'], 5000)
        self.assertEqual(record['anchor_at'], '2026-10-29T15:00:00+00:00'); self.assertEqual((record['pages'], record['has_more']), (1, False))

    def test_duplicate_weeks_keep_the_later_entry_and_a_missing_week_is_zero_filled(self):
        s0, s1, s2 = dt.date(2026, 10, 4), dt.date(2026, 10, 11), dt.date(2026, 10, 18)
        weeks = [week(s2, [1] * 7), week(s0, [9] * 7), week(s2, [2] * 7)]
        record = fold(weeks, '2026-10-24T05:00:00Z', 1, pages=2, has_more=True)
        self.assertEqual(record['days'], [9] * 7 + [0] * 7 + [2] * 6)  # Saturday the 24th 22:00 PDT = through 2026-10-24

    def test_an_empty_history_has_no_start(self):
        record = fold([], '2026-10-24T05:00:00Z', 0, pages=1, has_more=False)
        self.assertIsNone(record['start']); self.assertEqual(record['days'], [])
        self.assertIsNone(stars_between(record, dt.date(2026, 1, 1), dt.date(2026, 1, 2)))

    def test_stars_between_clips_to_the_record_and_refuses_ranges_it_cannot_serve(self):
        record = {'start': '2026-10-01', 'through': '2026-10-10', 'days': list(range(1, 11)), 'has_more': False}
        self.assertEqual(stars_between(record, dt.date(2026, 10, 3), dt.date(2026, 10, 5)), 3 + 4 + 5)
        self.assertEqual(stars_between(record, dt.date(2026, 9, 1), dt.date(2026, 10, 1)), 1)  # before the start, no older history: zero stars there
        self.assertEqual(stars_between(record, dt.date(2026, 10, 11), dt.date(2026, 10, 10)), 0)
        self.assertIsNone(stars_between(record, dt.date(2026, 10, 3), dt.date(2026, 10, 11)))  # after through
        self.assertIsNone(stars_between(dict(record, has_more=True), dt.date(2026, 9, 30), dt.date(2026, 10, 2)))  # older history unread
        self.assertEqual(stars_between(dict(record, has_more=True), dt.date(2026, 10, 1), dt.date(2026, 10, 2)), 3)


class FakeWorldHistoryTests(unittest.TestCase):
    """The fake's history agrees with its own counts the way GitHub's does (sum of days = count), and read_pages pages it."""
    def test_history_sum_equals_the_count_and_pages_stop_at_the_end(self):
        world = make_world(repos=5, seed=3)
        clock = FakeClock()
        gh = GitHub('t', fetch=world.fetch, sleeper=clock.sleep, clock=clock)
        for node_id, repo in world.repos.items():
            pages = read_pages(gh, repo['full_name'], pages=100)
            weeks = [w for _, ws, _ in pages for w in ws]
            self.assertEqual(sum(sum(w['days']) for w in weeks), world.stars(node_id, world.now))
            self.assertEqual(len(weeks), len(world.history_weeks(node_id, world.now)))
            self.assertFalse(pages[-1][2]); self.assertTrue(all(has_next for _, _, has_next in pages[:-1]))
            self.assertEqual([len(ws) for _, ws, _ in pages[:-1]], [30] * (len(pages) - 1))
            record = fold(weeks, world.now, world.stars(node_id, world.now), pages=len(pages), has_more=False)
            self.assertEqual(sum(record['days']), record['anchor_stars'])  # nothing after the anchor day is kept


if __name__ == '__main__':
    unittest.main()
