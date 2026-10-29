"""The page-ready windows file: snapshot diffs once the ring is deep enough, history before that, per-row fallback, nulls."""
import datetime as dt, unittest
from capture.windows import build_page, column, history_gain

D = dt.date(2026, 11, 10)
REPOS = {'a': {'full_name': 'o/a', 'language': 'Go', 'created_at': '2020-01-01T00:00:00Z'}, 'b': {'full_name': 'o/b', 'language': None, 'created_at': None},
         'c': {'full_name': 'o/c'}, 'low': {'full_name': 'o/low'}}


def record(start, through, days, anchor_stars, has_more=False):
    return {'start': start, 'through': through, 'days': days, 'anchor_at': through + 'T22:00:00+00:00', 'anchor_stars': anchor_stars, 'pages': 2, 'has_more': has_more}


class WindowsTests(unittest.TestCase):
    def test_snapshot_windows_use_the_newest_snapshot_at_or_before_d_minus_n(self):
        snaps = {dt.date(2026, 11, 2): {'a': 100, 'b': 50}, dt.date(2026, 11, 9): {'a': 130, 'b': 60, 'c': 10}, D: {'a': 150, 'b': 70, 'c': 20}}
        counts = {'a': 150, 'b': 70, 'c': 20, 'low': 5}
        page = build_page(D, '2026-11-11T05:30:00+00:00', 10, REPOS, counts, sorted(snaps), snaps.get, {}, dt.date(2026, 11, 2))
        self.assertEqual(page['columns']['node_id'], ['a', 'b', 'c']); self.assertEqual(page['row_count'], 3)  # sorted by stars, the threshold applied
        self.assertEqual(page['columns']['full_name'], ['o/a', 'o/b', 'o/c']); self.assertEqual(page['columns']['language'], ['Go', None, None])
        one, seven = page['windows'][0], page['windows'][1]
        self.assertEqual((one['source'], one['baseline_date'], one['exact_since'], one['column']), ('snapshot', '2026-11-09', '2026-11-03', 'gain_1d'))
        self.assertEqual(page['columns']['gain_1d'], [20, 10, 10])
        self.assertEqual((seven['source'], seven['baseline_date'], seven['exact_since']), ('snapshot', '2026-11-02', '2026-11-09'))  # 11-03 missing: the 11-02 snapshot serves
        self.assertEqual(page['columns']['gain_7d'], [50, 20, None]); self.assertEqual(seven['rows_snapshot'], 2); self.assertEqual(seven['rows_null'], 1)
        fifteen = page['windows'][2]
        self.assertEqual((fifteen['source'], fifteen['baseline_date'], fifteen['exact_since']), ('history', None, '2026-11-17'))
        self.assertEqual(page['columns']['gain_15d'], [None] * 3); self.assertEqual(page['columns']['history_mask'], [0, 0, 0])
        self.assertEqual(page['first_snapshot_date'], '2026-11-02'); self.assertEqual(page['threshold'], 10); self.assertEqual(page['date'], '2026-11-10')
        self.assertEqual([w['days'] for w in page['windows']], [1, 7, 15, 30, 90, 180, 365])
        self.assertEqual(list(page['columns']), ['node_id', 'full_name', 'language', 'created_at', 'stars'] + [column(n) for n in (1, 7, 15, 30, 90, 180, 365)] + ['history_mask'])

    def test_history_windows_add_the_days_in_the_window_to_the_change_since_the_anchor(self):
        # history read on 11-08 (anchor 11-08 at 900 stars), the day buckets 11-01..11-08 are 1..8 (the 8th partial); today 950
        rec = record('2026-11-01', '2026-11-08', [1, 2, 3, 4, 5, 6, 7, 8], 900)
        self.assertEqual(history_gain(rec, D, 7, 950), (4 + 5 + 6 + 7 + 8) + 50)  # window 11-04..11-10
        self.assertEqual(history_gain(rec, D, 30, 950), sum(range(1, 9)) + 50)  # the record starts after the window: no older history exists
        self.assertIsNone(history_gain(dict(rec, has_more=True), D, 30, 950))  # older history exists but was not read
        self.assertIsNone(history_gain(rec, D, 1, 950))  # the anchor (11-08) is older than the 1-day window (11-10)
        self.assertEqual(history_gain(rec, dt.date(2026, 11, 8), 1, 905), 8 + 5)  # on the anchor day the 1-day window is that day
        self.assertIsNone(history_gain(rec, dt.date(2026, 11, 7), 7, 905))  # a page dated before the anchor cannot use it
        self.assertIsNone(history_gain(None, D, 7, 1)); self.assertIsNone(history_gain({'start': None, 'days': [], 'through': '2026-11-08', 'anchor_stars': 0}, D, 7, 1))

    def test_rows_fall_back_to_history_and_set_their_mask_bit(self):
        snaps = {dt.date(2026, 11, 9): {'a': 130}, D: {'a': 150, 'b': 70}}
        history = {'b': record('2026-10-01', '2026-11-10', [1] * 41, 70)}  # b entered today: history through today, anchor = today's count
        page = build_page(D, 'now', 10, REPOS, {'a': 150, 'b': 70}, sorted(snaps), snaps.get, history, dt.date(2026, 11, 9))
        self.assertEqual(page['columns']['gain_1d'], [20, 1]); self.assertEqual(page['columns']['gain_7d'], [None, 7]); self.assertEqual(page['columns']['gain_30d'], [None, 30])
        self.assertEqual(page['columns']['gain_365d'], [None, 41])  # the record starts after the window start with no older history: all its days
        self.assertEqual(page['columns']['history_mask'], [0, 0b1111111])
        one = page['windows'][0]
        self.assertEqual((one['source'], one['rows_snapshot'], one['rows_history'], one['rows_null']), ('snapshot', 1, 1, 0))
        self.assertEqual(page['windows'][1]['rows_null'], 1)

    def test_no_snapshots_yet_means_every_window_is_history_without_exact_since(self):
        page = build_page(D, 'now', 10, REPOS, {'a': 150}, [], lambda d: None, {}, None)
        self.assertTrue(all(w['source'] == 'history' and w['exact_since'] is None for w in page['windows'])); self.assertIsNone(page['first_snapshot_date'])


if __name__ == '__main__':
    unittest.main()
