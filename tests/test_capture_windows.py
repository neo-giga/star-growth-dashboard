"""The page-ready windows file: snapshot diffs once the ring is deep enough, history before that, per-row fallback, nulls."""
import datetime as dt, unittest
from capture.history import PACIFIC, WINDOWS, pacific_date
from capture.windows import build_page, column, history_gain

D = dt.date(2026, 11, 10)
T = dt.datetime(2026, 11, 11, 5, 30, tzinfo=dt.timezone.utc)  # D's capture: 21:30 PST
REPOS = {'a': {'full_name': 'o/a', 'language': 'Go', 'created_at': '2020-01-01T00:00:00Z'}, 'b': {'full_name': 'o/b', 'language': None, 'created_at': None},
         'c': {'full_name': 'o/c'}, 'low': {'full_name': 'o/low'}}


def record(start, through, days, anchor_stars, has_more=False):
    return {'start': start, 'through': through, 'days': days, 'anchor_at': through + 'T22:00:00+00:00', 'anchor_stars': anchor_stars, 'pages': 2, 'has_more': has_more}


class WindowsTests(unittest.TestCase):
    def test_snapshot_windows_use_the_snapshot_dated_d_minus_n(self):
        snaps = {dt.date(2026, 11, 2): {'a': 100, 'b': 50}, dt.date(2026, 11, 9): {'a': 130, 'b': 60, 'c': 10}, D: {'a': 150, 'b': 70, 'c': 20}}
        counts = {'a': 150, 'b': 70, 'c': 20, 'low': 5}
        page = build_page(D, T.isoformat(), 10, REPOS, counts, sorted(snaps), snaps.get, {}, dt.date(2026, 11, 2))
        self.assertEqual(page['columns']['node_id'], ['a', 'b', 'c']); self.assertEqual(page['row_count'], 3)  # sorted by stars, the threshold applied
        self.assertEqual(page['columns']['full_name'], ['o/a', 'o/b', 'o/c']); self.assertEqual(page['columns']['language'], ['Go', None, None])
        one, seven = page['windows'][0], page['windows'][1]
        self.assertEqual((one['source'], one['baseline_date'], one['exact_since'], one['column']), ('snapshot', '2026-11-09', '2026-11-03', 'gain_1d'))
        self.assertEqual(page['columns']['gain_1d'], [20, 10, 10])
        self.assertEqual((seven['source'], seven['baseline_date'], seven['exact_since']), ('history', None, '2026-11-09'))  # 11-03 missing: not served by 11-02's
        self.assertEqual(page['columns']['gain_7d'], [None, None, None]); self.assertEqual(seven['rows_snapshot'], 0); self.assertEqual(seven['rows_null'], 3)
        fifteen = page['windows'][2]
        self.assertEqual((fifteen['source'], fifteen['baseline_date'], fifteen['exact_since']), ('history', None, '2026-11-17'))
        self.assertEqual(page['columns']['gain_15d'], [None] * 3); self.assertEqual(page['columns']['history_mask'], [0, 0, 0])
        self.assertEqual(page['first_snapshot_date'], '2026-11-02'); self.assertEqual(page['threshold'], 10); self.assertEqual(page['date'], '2026-11-10')
        self.assertEqual([w['days'] for w in page['windows']], [1, 7, 15, 30, 90, 180, 365])
        self.assertEqual(list(page['columns']), ['node_id', 'full_name', 'language', 'created_at', 'stars'] + [column(n) for n in (1, 7, 15, 30, 90, 180, 365)] + ['history_mask'])

    def test_history_windows_add_the_days_in_the_window_to_the_change_since_the_anchor(self):
        # history read on 11-08 (anchor 11-08 22:00 UTC = 14:00 PST, 900 stars), the day buckets 11-01..11-08 are 1..8 (the 8th partial); today 950
        rec = record('2026-11-01', '2026-11-08', [1, 2, 3, 4, 5, 6, 7, 8], 900)
        self.assertEqual(history_gain(rec, T, 7, 950), round(3 * 2.5 / 24 + (4 + 5 + 6 + 7 + 8) + 50))  # from 11-03 21:30 PST: 2.5 h of 11-03, then 11-04..11-08
        self.assertEqual(history_gain(rec, T, 30, 950), sum(range(1, 9)) + 50)  # the record starts after the window: no older history exists
        self.assertIsNone(history_gain(dict(rec, has_more=True), T, 30, 950))  # older history exists but was not read
        self.assertIsNone(history_gain(rec, T, 1, 950))  # the anchor (11-08 14:00 PST) is older than the 1-day window's start (11-09 21:30 PST)
        on_anchor_day = dt.datetime(2026, 11, 9, 5, 30, tzinfo=dt.timezone.utc)  # 11-08 21:30 PST, after the anchor
        self.assertEqual(history_gain(rec, on_anchor_day, 1, 905), round(7 * 2.5 / 24 + 8 + 5))
        before_anchor = dt.datetime(2026, 11, 8, 5, 30, tzinfo=dt.timezone.utc)  # a capture before the history was read: the anchor term subtracts the stars since
        self.assertEqual(history_gain(rec, before_anchor, 1, 895), round(6 * 2.5 / 24 + 7 + 8 + 895 - 900))
        self.assertIsNone(history_gain(None, T, 7, 1)); self.assertIsNone(history_gain({'start': None, 'days': [], 'through': '2026-11-08', 'anchor_stars': 0}, T, 7, 1))

    def test_rows_fall_back_to_history_and_set_their_mask_bit(self):
        snaps = {dt.date(2026, 11, 9): {'a': 130}, D: {'a': 150, 'b': 70}}
        history = {'b': record('2026-10-01', '2026-11-10', [1] * 41, 70)}  # b entered today: history through today, anchor = today's count
        page = build_page(D, T.isoformat(), 10, REPOS, {'a': 150, 'b': 70}, sorted(snaps), snaps.get, history, dt.date(2026, 11, 9))
        self.assertEqual(page['columns']['gain_1d'], [20, 1]); self.assertEqual(page['columns']['gain_7d'], [None, 7]); self.assertEqual(page['columns']['gain_30d'], [None, 30])
        self.assertEqual(page['columns']['gain_365d'], [None, 41])  # the record starts after the window start with no older history: all its days
        self.assertEqual(page['columns']['history_mask'], [0, 0b1111111])
        one = page['windows'][0]
        self.assertEqual((one['source'], one['rows_snapshot'], one['rows_history'], one['rows_null']), ('snapshot', 1, 1, 0))
        self.assertEqual(page['windows'][1]['rows_null'], 1)

    def test_no_snapshots_yet_means_every_window_is_history_without_exact_since(self):
        page = build_page(D, T.isoformat(), 10, REPOS, {'a': 150}, [], lambda d: None, {}, None)
        self.assertTrue(all(w['source'] == 'history' and w['exact_since'] is None for w in page['windows'])); self.assertIsNone(page['first_snapshot_date'])

    def test_a_missed_capture_day_falls_back_to_history_never_to_an_older_snapshot(self):
        # 11-09 failed: the 1-day window must not become 11-08..11-10 under the label "1 day"
        snaps = {dt.date(2026, 11, 8): {'a': 100, 'b': 50}, D: {'a': 150, 'b': 70}}
        history = {'a': record('2026-10-01', '2026-11-10', [1] * 41, 150)}  # a has history anchored today; b has none
        page = build_page(D, T.isoformat(), 10, REPOS, {'a': 150, 'b': 70}, sorted(snaps), snaps.get, history, dt.date(2026, 11, 8))
        one = page['windows'][0]
        self.assertEqual((one['source'], one['baseline_date'], one['rows_history'], one['rows_null']), ('history', None, 1, 1))
        self.assertEqual(page['columns']['gain_1d'], [1, None]); self.assertEqual([m & 1 for m in page['columns']['history_mask']], [1, 0])
        two = build_page(D + dt.timedelta(days=1), (T + dt.timedelta(days=1)).isoformat(), 10, REPOS, {'a': 152, 'b': 71}, sorted(snaps) + [D + dt.timedelta(days=1)],
                         {**snaps, D + dt.timedelta(days=1): {'a': 152, 'b': 71}}.get, history, dt.date(2026, 11, 8))
        self.assertEqual((two['windows'][0]['baseline_date'], two['columns']['gain_1d']), ('2026-11-10', [2, 1]))  # the next day is whole again

    def test_a_column_does_not_jump_when_it_flips_from_history_to_snapshot_in_pdt_pst_and_across_dst(self):
        # A constant 10 stars an hour; captures at 05:30 UTC (22:30 PDT, 21:30 PST); the backfill anchored 2 h before the first capture.
        epoch = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        count = lambda t: int(10 * (t - epoch).total_seconds() / 3600)
        capture = lambda d: dt.datetime(d.year, d.month, d.day, 5, 30, tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
        midnight = lambda d: dt.datetime.combine(d, dt.time(), PACIFIC).astimezone(dt.timezone.utc)
        checked = 0
        for offset in range(23):  # first snapshots from 10-20 to 11-11: flips in PDT, in PST, and windows spanning the 11-01 change
            first = dt.date(2026, 10, 20) + dt.timedelta(days=offset)
            anchor = capture(first) - dt.timedelta(hours=2)
            start, through = dt.date(2025, 9, 28), pacific_date(anchor)
            days = [count(min(midnight(start + dt.timedelta(days=i + 1)), anchor)) - count(midnight(start + dt.timedelta(days=i))) for i in range((through - start).days + 1)]
            rec = {'start': start.isoformat(), 'days': days, 'through': through.isoformat(), 'anchor_at': anchor.isoformat(), 'anchor_stars': count(anchor),
                   'pages': 2, 'has_more': False}
            for index, n in enumerate(WINDOWS):
                values = []
                for date in (first + dt.timedelta(days=n - 1), first + dt.timedelta(days=n)):  # the last history day, the first snapshot day
                    dates = [first + dt.timedelta(days=i) for i in range((date - first).days + 1)]
                    page = build_page(date, capture(date).isoformat(), 10, REPOS, {'a': count(capture(date))}, dates, lambda d: {'a': count(capture(d))},
                                      {'a': rec}, first)
                    values.append((page['windows'][index]['source'], page['columns'][column(n)][0]))
                (before, history), (after, snapshot) = values
                self.assertEqual((before, after), ('history', 'snapshot'), (first, n))
                self.assertEqual(snapshot, 240 * n, (first, n))
                self.assertLessEqual(abs(history - snapshot), 0.05 * snapshot, (first, n, history, snapshot))
                checked += 1
        self.assertEqual(checked, 23 * 7)

    def test_history_gains_never_exceed_the_stars_or_shrink_as_the_window_grows(self):
        # Gathin23/cleanyourtv on 2026-10-29: one star arrived between its anchor and its history read, so the days sum to 45,328
        # while the anchor (and today's count) is 45,327; the r2 file showed gain_15d..gain_365d = 45,328 > stars.
        rec = {'start': '2026-10-11', 'days': [0, 0, 0, 0, 0, 0, 493, 2115, 4372, 6812, 6788, 4608, 3429, 3815, 3842, 3637, 2435, 1877, 1105],
               'through': '2026-10-29', 'anchor_at': '2026-10-29T21:20:06.915933+00:00', 'anchor_stars': 45327, 'pages': 1, 'has_more': False}
        unstarred = dict(rec, days=[0] * 18 + [3], anchor_stars=1000)  # 3 stars today, then 10 unstars after the anchor
        date = dt.date(2026, 10, 29)
        for node_id, stars, record in (('a', 45327, rec), ('b', 990, unstarred)):
            page = build_page(date, '2026-10-29T20:11:22.178972+00:00', 10, REPOS, {node_id: stars}, [], lambda d: None, {node_id: record}, None)
            gains = [page['columns'][column(n)][0] for n in WINDOWS]
            self.assertEqual(page['columns']['history_mask'], [127])
            self.assertTrue(all(0 <= g <= stars for g in gains), gains); self.assertEqual(gains, sorted(gains))

if __name__ == '__main__':
    unittest.main()
