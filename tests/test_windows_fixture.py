"""tests/fixtures/windows-sample.json: a real 200-row extract of windows/latest.json in exactly the interface shape of docs/data-layout.md."""
import json, pathlib, re, unittest
from capture.history import WINDOWS
from capture.windows import PAGE_SCHEMA, column
ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/windows-sample.json').read_text())
DOC = (ROOT / 'docs/data-layout.md').read_text()


class WindowsFixtureTests(unittest.TestCase):
    def test_shape_matches_the_documented_interface(self):
        self.assertEqual(list(FIXTURE), ['schema', 'generated_at', 'date', 'threshold', 'row_count', 'first_snapshot_date', 'windows', 'columns'])
        self.assertEqual(FIXTURE['schema'], PAGE_SCHEMA); self.assertIn(PAGE_SCHEMA, DOC)
        self.assertRegex(FIXTURE['date'], r'^\d{4}-\d{2}-\d{2}$'); self.assertRegex(FIXTURE['generated_at'], r'^\d{4}-\d{2}-\d{2}T')
        names = ['node_id', 'full_name', 'language', 'created_at', 'stars'] + [column(n) for n in WINDOWS] + ['history_mask']
        self.assertEqual(list(FIXTURE['columns']), names)
        for name in names:
            self.assertEqual(len(FIXTURE['columns'][name]), FIXTURE['row_count'], name)
            self.assertIn(name, DOC)
        self.assertEqual(FIXTURE['row_count'], 200)
        self.assertEqual([w['days'] for w in FIXTURE['windows']], list(WINDOWS))
        for index, window in enumerate(FIXTURE['windows']):
            self.assertEqual(list(window), ['days', 'column', 'source', 'exact_since', 'baseline_date', 'rows_snapshot', 'rows_history', 'rows_null'])
            self.assertEqual(window['column'], column(window['days'])); self.assertIn(window['source'], ('history', 'snapshot'))
            self.assertEqual(window['rows_snapshot'] + window['rows_history'] + window['rows_null'], FIXTURE['row_count'])
            gains = FIXTURE['columns'][window['column']]
            self.assertEqual(sum(1 for g in gains if g is None), window['rows_null'])
            self.assertEqual(sum(1 for m in FIXTURE['columns']['history_mask'] if m & (1 << index)), window['rows_history'])

    def test_rows_are_real_repos_above_the_threshold_sorted_by_stars(self):
        stars = FIXTURE['columns']['stars']
        self.assertEqual(stars, sorted(stars, reverse=True)); self.assertGreater(min(stars), FIXTURE['threshold'])
        for node_id, full_name in zip(FIXTURE['columns']['node_id'], FIXTURE['columns']['full_name']):
            self.assertRegex(node_id, r'^[A-Za-z0-9_=-]+$'); self.assertRegex(full_name, r'^[^/\s]+/[^/\s]+$')
        self.assertEqual(len(set(FIXTURE['columns']['node_id'])), 200)
        for gain in (FIXTURE['columns']['gain_1d'], FIXTURE['columns']['gain_365d']):
            self.assertTrue(any(g is not None for g in gain))
        # a longer window never shows fewer stars than a shorter one from the same source (history counts stars still present)
        for row in range(200):
            values = [FIXTURE['columns'][column(n)][row] for n in WINDOWS if FIXTURE['columns'][column(n)][row] is not None]
            if FIXTURE['columns']['history_mask'][row] == 127:
                self.assertEqual(values, sorted(values), FIXTURE['columns']['full_name'][row])


if __name__ == '__main__':
    unittest.main()
