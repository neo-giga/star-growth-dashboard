"""Publishing the page from windows/latest.json: the gate (age, coverage, nulls), the page file, the agent feed v2, the staged
site and the hashed static release. Network-free; the real 200-row fixture is the data."""
import copy, datetime as dt, hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile, unittest
from publish import stage as st
from publish.agent_feed import build_agent_feed, record
ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/windows-sample.json').read_text())
NOW = dt.datetime.fromisoformat(FIXTURE['generated_at']) + dt.timedelta(hours=2)
SITE_FILES = ('index.html', 'app.js', 'style.css', 'llms.txt', '.nojekyll', 'docs/agent-feed.md', 'docs/publish-page.yml')


def diagnostic(total=200, date=None):
    return {'schema': 'star-growth-dashboard/capture-diagnostic/1', 'date': date or FIXTURE['date'], 'coverage': {'rows_above_threshold': 200, 'total_count': total, 'total_count_source': 'search'}}


def data_dir(tmp, doc=None, diag=diagnostic(), name='data'):
    root = pathlib.Path(tmp) / name
    (root / 'windows').mkdir(parents=True); (root / 'diagnostics' / FIXTURE['date'][:4]).mkdir(parents=True)
    (root / 'windows/latest.json').write_text(json.dumps(doc or FIXTURE))
    if diag is not None:
        (root / 'diagnostics' / FIXTURE['date'][:4] / f'{FIXTURE["date"]}.json').write_text(json.dumps(diag))
    return root


def checkout(tmp):
    site = pathlib.Path(tmp) / 'checkout'
    for name in SITE_FILES:
        (site / name).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(ROOT / name, site / name)
    return site


class GateTests(unittest.TestCase):
    def test_the_fixture_is_valid_and_passes_at_full_coverage(self):
        st.validate_windows(FIXTURE)
        problems, measured = st.gate(FIXTURE, diagnostic(), NOW)
        self.assertEqual(problems, []); self.assertEqual(measured['row_count'], 200); self.assertEqual(measured['coverage'], 1.0)
        self.assertEqual(measured['age_hours'], 2.0); self.assertEqual(measured['null_share'], {c: 0.0 for c in st.GAIN_COLUMNS})

    def test_validation_rejects_a_foreign_or_ragged_file(self):
        for doc, message in ((dict(FIXTURE, schema='x'), 'schema'), (dict(FIXTURE, row_count=199), 'row_count'), (dict(FIXTURE, columns={k: v for k, v in FIXTURE['columns'].items() if k != 'node_id'}), 'node_id'),
                             (dict(FIXTURE, windows=FIXTURE['windows'][:6]), 'seven gain columns'), (dict(FIXTURE, generated_at='2026-10-29T20:13:43'), 'timezone'),
                             (dict(FIXTURE, columns=dict(FIXTURE['columns'], gain_7d=['5'] + FIXTURE['columns']['gain_7d'][1:])), 'integers or null')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                st.validate_windows(doc)

    def test_an_empty_or_repeated_node_id_or_stars_at_or_below_the_threshold_is_refused_before_upload(self):
        empty, repeated, below = copy.deepcopy(FIXTURE), copy.deepcopy(FIXTURE), copy.deepcopy(FIXTURE)
        empty['columns']['node_id'][7] = ''; repeated['columns']['node_id'][7] = repeated['columns']['node_id'][3]; below['columns']['stars'][7] = FIXTURE['threshold']
        for name, doc, message in (('empty', empty, 'nonempty string'), ('repeated', repeated, 'node_id repeats: 1 rows'), ('below', below, '1 rows have stars at or below the threshold 2000')):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, message):
                    st.validate_windows(doc)
                with self.assertRaisesRegex(ValueError, message):
                    st.stage_site(checkout(tmp), data_dir(tmp, doc), pathlib.Path(tmp) / 'out', now=NOW, ignore_gate=True)
                self.assertFalse((pathlib.Path(tmp) / 'out').exists())

    def test_a_file_older_than_36_hours_is_refused(self):
        problems, measured = st.gate(FIXTURE, diagnostic(), dt.datetime.fromisoformat(FIXTURE['generated_at']) + dt.timedelta(hours=36, minutes=1))
        self.assertEqual(len(problems), 1); self.assertIn('over 36 h', problems[0]); self.assertEqual(measured['age_hours'], 36.02)
        self.assertEqual(st.gate(FIXTURE, diagnostic(), dt.datetime.fromisoformat(FIXTURE['generated_at']) + dt.timedelta(hours=35, minutes=59))[0], [])

    def test_a_row_count_under_99_percent_of_the_search_total_is_refused(self):
        self.assertEqual(st.gate(FIXTURE, diagnostic(total=202), NOW)[0], [])  # 200/202 = 99.01%
        problems, measured = st.gate(FIXTURE, diagnostic(total=203), NOW)  # 200/203 = 98.5%
        self.assertEqual(len(problems), 1); self.assertIn('under 99% of the search total 203', problems[0]); self.assertEqual(measured['coverage'], 0.9852)
        for diag in (None, {}, diagnostic(date='2026-10-28'), diagnostic(total=None), diagnostic(total=0)):  # no total for the day: fail closed
            with self.subTest(diag=diag):
                problems, _ = st.gate(FIXTURE, diag, NOW)
                self.assertEqual(len(problems), 1); self.assertIn('no search total', problems[0])

    def test_a_gain_column_null_for_more_than_1_percent_of_rows_is_refused(self):
        doc = copy.deepcopy(FIXTURE)
        doc['columns']['gain_30d'][:2] = [None, None]  # 1.0%: allowed
        self.assertEqual(st.gate(doc, diagnostic(), NOW)[0], [])
        doc['columns']['gain_30d'][2] = None  # 1.5%
        problems, measured = st.gate(doc, diagnostic(), NOW)
        self.assertEqual(len(problems), 1); self.assertIn('gain_30d is null for 3 of 200 rows (1.5%)', problems[0]); self.assertEqual(measured['null_share']['gain_30d'], 0.015)
        doc['columns']['gain_1d'] = [None] * 200
        self.assertEqual([p.split(' is null')[0] for p in st.gate(doc, diagnostic(), NOW)[0]], ['gain_1d', 'gain_30d'])

    def test_a_column_nulled_by_a_missed_capture_is_exempt_only_on_or_after_its_exact_since(self):
        # PR A r3: when the snapshot N days back is missing, the N-day column is history-sourced with baseline_date null and mostly
        # null that day. From exact_since on that can only be a missed capture: exempt. Before it the rule holds (the backfill gate).
        doc = copy.deepcopy(FIXTURE); doc['columns']['gain_7d'] = [None] * 200  # gain_7d: exact_since 2026-11-05, baseline_date null
        doc['windows'][0] = dict(doc['windows'][0], source='snapshot', baseline_date='2026-11-03')  # 1 d has its baseline on these days
        for date, refused in (('2026-11-04', True), ('2026-11-05', False), ('2026-11-06', False)):
            with self.subTest(date=date):
                problems, measured = st.gate(dict(doc, date=date), diagnostic(date=date), NOW)
                self.assertEqual(problems, ['gain_7d is null for 200 of 200 rows (100.0%), over 1%'] if refused else [])
                self.assertEqual(measured['null_share']['gain_7d'], 1.0); self.assertEqual(measured['null_exempt'], [] if refused else ['gain_7d'])
        snapshot = copy.deepcopy(doc); snapshot['windows'][1] = dict(doc['windows'][1], source='snapshot', baseline_date='2026-10-30')
        self.assertEqual(len(st.gate(dict(snapshot, date='2026-11-06'), diagnostic(date='2026-11-06'), NOW)[0]), 1)  # a baseline was read: the rule holds

    def test_every_problem_is_listed_at_once(self):
        doc = copy.deepcopy(FIXTURE); doc['columns']['gain_365d'][:5] = [None] * 5
        problems, _ = st.gate(doc, diagnostic(total=250), NOW + dt.timedelta(days=2))
        self.assertEqual([p.split(' ')[0] for p in problems], ['windows', 'row_count', 'gain_365d'])


class PageFileTests(unittest.TestCase):
    def test_the_page_file_is_the_interface_without_node_ids(self):
        page = st.page_file(FIXTURE)
        self.assertEqual(page['schema'], 'star-growth-dashboard/page/1'); self.assertNotIn('node_id', page['columns'])
        self.assertEqual(list(page['columns']), ['full_name', 'language', 'created_at', 'stars', *st.GAIN_COLUMNS, 'history_mask'])
        for key in ('generated_at', 'date', 'threshold', 'row_count', 'first_snapshot_date', 'windows'):
            self.assertEqual(page[key], FIXTURE[key])
        self.assertIs(page['columns']['gain_7d'], FIXTURE['columns']['gain_7d'])


class AgentFeedTests(unittest.TestCase):
    def test_records_carry_gains_and_their_source_per_window(self):
        doc = copy.deepcopy(FIXTURE); doc['columns']['history_mask'][0] = 0b0000101; doc['columns']['gain_15d'][0] = None
        r = record(doc, 0)
        self.assertEqual(r['full_name'], 'p0deje/Maccy'); self.assertEqual(r['github_url'], 'https://github.com/p0deje/Maccy'); self.assertEqual(r['stars'], 22138)
        self.assertEqual(list(r['gains']), ['1d', '7d', '15d', '30d', '90d', '180d', '365d']); self.assertEqual(r['gains']['7d'], 109); self.assertIsNone(r['gains']['15d'])
        self.assertEqual(r['gain_source'], {'1d': 'history', '7d': 'snapshot', '15d': None, '30d': 'snapshot', '90d': 'snapshot', '180d': 'snapshot', '365d': 'snapshot'})
        self.assertEqual(record(FIXTURE, 1)['gain_source']['365d'], 'history')

    def test_the_feed_is_content_addressed_listed_in_its_manifest_and_ranked_per_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = build_agent_feed(FIXTURE, tmp)
            root = pathlib.Path(tmp) / 'api/v2'
            columns = (root / 'snapshots' / catalog['snapshot_id'] / 'columns.json').read_bytes()
            self.assertEqual(hashlib.sha256(columns).hexdigest(), catalog['snapshot_id']); self.assertEqual(json.loads(columns), FIXTURE)
            self.assertEqual(json.loads((root / 'catalog.json').read_text()), {k: v for k, v in catalog.items() if k != 'catalog_url'})
            self.assertEqual(catalog['catalog_url'], 'https://neo-giga.github.io/star-growth-dashboard/api/v2/catalog.json'); self.assertEqual(catalog['schema_version'], '2.0')
            self.assertEqual(catalog['row_count'], 200); self.assertEqual(catalog['date'], FIXTURE['date']); self.assertEqual(catalog['windows'], FIXTURE['windows'])
            manifest = json.loads((root / 'snapshots' / catalog['snapshot_id'] / 'manifest.json').read_text())
            self.assertEqual(manifest['manifest_url'] if 'manifest_url' in manifest else catalog['manifest_url'], catalog['manifest_url'])
            listed = {f['path'] for f in manifest['files']}
            self.assertEqual(listed, {'columns.json', 'all.jsonl', 'all.csv', 'schema.json', *(f'top100/{c}.{ext}' for c in st.GAIN_COLUMNS for ext in ('json', 'md'))})
            for entry in manifest['files']:
                data = (root / 'snapshots' / catalog['snapshot_id'] / entry['path']).read_bytes()
                self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (entry['bytes'], entry['sha256']), entry['path'])
                self.assertTrue(entry['url'].startswith(catalog['manifest_url'].rsplit('/', 1)[0] + '/'))
            lines = (root / 'snapshots' / catalog['snapshot_id'] / 'all.jsonl').read_text().splitlines()
            self.assertEqual(len(lines), 200); self.assertEqual(json.loads(lines[0])['full_name'], 'p0deje/Maccy')
            top = json.loads((root / 'snapshots' / catalog['snapshot_id'] / 'top100/gain_365d.json').read_text())
            gains = [r['gains']['365d'] for r in top['rows']]
            self.assertEqual(len(gains), 100); self.assertEqual(gains, sorted(gains, reverse=True)); self.assertEqual(gains[0], max(FIXTURE['columns']['gain_365d']))
            self.assertEqual(top['window'], FIXTURE['windows'][6]); self.assertEqual(top['sort'], 'gain_365d_desc_null_excluded')
            md = (root / 'snapshots' / catalog['snapshot_id'] / 'top100/gain_7d.md').read_text()
            self.assertIn('| Repository | Stars | Gain 7d | Source |', md); self.assertEqual(md.count('\n| '), 101)
            csv_head = (root / 'snapshots' / catalog['snapshot_id'] / 'all.csv').read_text().splitlines()[0]
            self.assertEqual(csv_head, 'node_id,full_name,github_url,language,created_at,stars,gain_1d,gain_7d,gain_15d,gain_30d,gain_90d,gain_180d,gain_365d,history_windows')
            schema = json.loads((root / 'schema.json').read_text())
            self.assertEqual(schema['$id'], 'star-growth-dashboard/agent-feed/2'); self.assertEqual(schema['properties']['gains']['required'], ['1d', '7d', '15d', '30d', '90d', '180d', '365d'])
            self.assertEqual((root / 'snapshots' / catalog['snapshot_id'] / 'schema.json').read_bytes(), (root / 'schema.json').read_bytes())
            self.assertFalse((root.parent / 'v1').exists())


class StageTests(unittest.TestCase):
    def test_the_staged_site_is_the_hashed_release_plus_the_feed_and_names_no_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, data = checkout(tmp), data_dir(tmp)
            result = st.stage_site(site, data, pathlib.Path(tmp) / 'out', now=NOW)
            out = pathlib.Path(tmp) / 'out'
            html = (out / 'index.html').read_text(); app = re.search(r'<script[^>]+src="([^"]+)"', html)[1]; css = re.search(r'href="([^"]+\.css)"', html)[1]
            self.assertRegex(app, r'^app\.[0-9a-f]{16}\.js$'); self.assertRegex(css, r'^style\.[0-9a-f]{16}\.css$')
            data_name = re.search(r"'(data\.[0-9a-f]{16}\.json)'", (out / app).read_text())[1]
            page = json.loads((out / data_name).read_text())
            self.assertEqual(page['schema'], 'star-growth-dashboard/page/1'); self.assertEqual(page['row_count'], 200); self.assertNotIn('node_id', page['columns'])
            self.assertEqual(page['agent_catalog_url'], 'https://neo-giga.github.io/star-growth-dashboard/api/v2/catalog.json'); self.assertEqual(page['agent_snapshot_id'], result['snapshot_id'])
            for name in ('.nojekyll', 'llms.txt', 'docs/agent-feed.md', 'docs/publish-page.yml', 'api/v2/catalog.json', 'api/v2/schema.json'):
                self.assertTrue((out / name).is_file(), name)
            self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(['.nojekyll', 'index.html', 'llms.txt', 'docs', 'api', app, css, data_name]))
            for path in out.rglob('*'):
                if path.is_file():
                    self.assertNotIn('gittrend', path.read_bytes().decode('utf-8', 'replace').lower(), path)
            self.assertEqual(result['status'] if 'status' in result else 'ok', 'ok'); self.assertEqual(result['row_count'], 200); self.assertEqual(result['gate_ignored'], [])
            self.assertEqual(result['measurements']['coverage'], 1.0); self.assertEqual(result['page_file_bytes'], (out / data_name).stat().st_size)
            self.assertEqual(result['artifact_bytes'], sum(p.stat().st_size for p in out.rglob('*') if p.is_file()))
            # the same inputs stage the same hashed names; a changed file changes the data and app names, not the stylesheet
            shutil.rmtree(out); st.stage_site(site, data, out, now=NOW)
            self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(['.nojekyll', 'index.html', 'llms.txt', 'docs', 'api', app, css, data_name]))
            doc = copy.deepcopy(FIXTURE); doc['columns']['stars'][0] += 1
            shutil.rmtree(out); st.stage_site(site, data_dir(tmp, doc, name='data2'), out, now=NOW)
            names = sorted(p.name for p in out.iterdir())
            self.assertIn(css, names); self.assertNotIn(app, names); self.assertNotIn(data_name, names)

    def test_the_previous_generation_is_carried_for_one_deploy_and_no_older_one(self):
        def assets(out):
            html = (out / 'index.html').read_text(); app = re.search(r'<script[^>]+src="([^"]+)"', html)[1]
            return app, re.search(r'href="([^"]+\.css)"', html)[1], re.search(r"'(data\.[0-9a-f]{16}\.json)'", (out / app).read_text())[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp); site = checkout(tmp); docs = [copy.deepcopy(FIXTURE) for _ in range(3)]
            for i, doc in enumerate(docs):
                doc['columns']['stars'][0] += i
            first = st.stage_site(site, data_dir(tmp, docs[0], name='d0'), tmp / 'out0', now=NOW)
            self.assertEqual(first['previous_generation'], [])
            second = st.stage_site(site, data_dir(tmp, docs[1], name='d1'), tmp / 'out1', now=NOW, previous=tmp / 'out0')
            old, new = assets(tmp / 'out0'), assets(tmp / 'out1')
            self.assertNotEqual(old[0], new[0]); self.assertNotEqual(old[2], new[2])
            self.assertEqual(second['previous_generation'], sorted(old)); self.assertIsNone(second['previous_generation_note'])
            for name in old:  # a cached index.html or an already-loaded script still finds its files
                self.assertEqual((tmp / 'out1' / name).read_bytes(), (tmp / 'out0' / name).read_bytes(), name)
            st.stage_site(site, data_dir(tmp, docs[2], name='d2'), tmp / 'out2', now=NOW, previous=tmp / 'out1')
            names = {p.name for p in (tmp / 'out2').iterdir()}
            self.assertLessEqual(set(new), names); self.assertNotIn(old[0], names); self.assertNotIn(old[2], names)  # one generation only
            # today's live page is the GitTrend-era one (no v3 page file, the name in its files): nothing is carried, and the result says why
            v2 = tmp / 'v2'; v2.mkdir(); app, css = 'app.e8403d73240a1990.js', 'style.82a1b69b09437942.css'
            (v2 / 'index.html').write_text(f'<link rel="icon" href="data:,"><link href="{css}"><script src="{app}"></script><a href="api/v1/catalog.json"></a>')
            windows = [f'data-{w}.{h}.json' for w, h in (('day', '18fd23d482942271'), ('week', 'b69ecdadbcb420d7'), ('month', '43d945287bfad983'))]
            (v2 / app).write_text("const NOTE = 'it\\'s'; " + ' '.join(f"'{n}'" for n in windows)); (v2 / css).write_text('body{}')  # an escaped quote first
            for name in windows:
                (v2 / name).write_text('{}')
            result = st.stage_site(site, data_dir(tmp, docs[0], name='d4'), tmp / 'out4', now=NOW, previous=v2)
            self.assertEqual(result['previous_generation'], []); self.assertIn('no v3 page file', result['previous_generation_note'])
            self.assertFalse({app, css, *windows} & {p.name for p in (tmp / 'out4').iterdir()})
            # a v3-shaped publication whose files name GitTrend is not carried either (case-insensitive)
            gt = tmp / 'gt'; shutil.copytree(tmp / 'out0', gt); (gt / old[0]).write_text((gt / old[0]).read_text() + '\n// GitTrend\n')
            result = st.stage_site(site, data_dir(tmp, docs[1], name='d5'), tmp / 'out5', now=NOW, previous=gt)
            self.assertEqual(result['previous_generation'], []); self.assertIn(f"'gittrend' in {old[0]}", result['previous_generation_note'])
            self.assertFalse({old[0], old[2]} & {p.name for p in (tmp / 'out5').iterdir()})  # the stylesheet is unchanged, so this build has it
            # an unreadable previous publication carries nothing, says why, and does not block the deploy
            result = st.stage_site(site, data_dir(tmp, docs[0], name='d3'), tmp / 'out3', now=NOW, previous=tmp / 'missing')
            self.assertEqual(result['previous_generation'], []); self.assertIn('previous generation unreadable', result['previous_generation_note'])

    def test_a_refused_gate_stages_nothing_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = checkout(tmp)
            with self.assertRaises(st.GateRefused) as caught:
                st.stage_site(site, data_dir(tmp, diag=diagnostic(total=300)), pathlib.Path(tmp) / 'out', now=NOW)
            self.assertIn('under 99%', caught.exception.problems[0]); self.assertEqual(caught.exception.measurements['search_total'], 300)
            self.assertFalse((pathlib.Path(tmp) / 'out').exists()); self.assertEqual([p.name for p in pathlib.Path(tmp).iterdir() if p.name.startswith('.publish-')], [])
            # local previews only: the problems are returned, the site is staged
            result = st.stage_site(site, data_dir(tmp, diag=diagnostic(total=300), name='data2'), pathlib.Path(tmp) / 'out', now=NOW, ignore_gate=True)
            self.assertEqual(len(result['gate_ignored']), 1); self.assertTrue((pathlib.Path(tmp) / 'out/index.html').is_file())

    def test_the_cli_exits_2_with_a_diagnostic_when_refused_and_0_when_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = checkout(tmp)
            run = lambda data, out: subprocess.run([sys.executable, str(ROOT / 'scripts/publish_page.py'), '--site-dir', str(site), '--data-dir', str(data), '--output-dir', str(pathlib.Path(tmp) / out),
                                                    '--diagnostic', str(pathlib.Path(tmp) / f'{out}.json'), '--now', NOW.isoformat()], capture_output=True, text=True)
            stale = copy.deepcopy(FIXTURE); stale['generated_at'] = (NOW - dt.timedelta(hours=40)).isoformat()
            proc = run(data_dir(tmp, stale, name='stale'), 'out-stale')
            self.assertEqual(proc.returncode, 2); self.assertIn('current site unchanged', proc.stderr); self.assertFalse((pathlib.Path(tmp) / 'out-stale').exists())
            diag = json.loads((pathlib.Path(tmp) / 'out-stale.json').read_text())
            self.assertEqual(diag['status'], 'refused'); self.assertIn('over 36 h', diag['problems'][0]); self.assertTrue(diag['current_site_preserved'])
            proc = run(pathlib.Path(tmp) / 'missing', 'out-missing')
            self.assertEqual(proc.returncode, 2); self.assertEqual(json.loads((pathlib.Path(tmp) / 'out-missing.json').read_text())['status'], 'failed')
            proc = run(data_dir(tmp, name='good'), 'out-good')
            self.assertEqual(proc.returncode, 0, proc.stderr); self.assertTrue((pathlib.Path(tmp) / 'out-good/index.html').is_file())
            self.assertEqual(json.loads((pathlib.Path(tmp) / 'out-good.json').read_text())['status'], 'ok')


class StaticReleaseTests(unittest.TestCase):
    def test_immutable_assets_link_as_one_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / 'src'; out = pathlib.Path(tmp) / 'out'; src.mkdir()
            (src / 'index.html').write_text('<link href="style.css"><script src="app.js"></script>'); (src / 'style.css').write_text('body{}')
            (src / 'app.js').write_text("const DATA_FILE = 'data.json';"); (src / 'data.json').write_text('{"version":1}')
            def build():
                subprocess.run([sys.executable, str(ROOT / 'scripts/build_static_release.py'), '--source', str(src), '--output', str(out)], check=True, capture_output=True)
                html = (out / 'index.html').read_text(); app = re.search(r'src="([^"]+)"', html)[1]; css = re.search(r'href="([^"]+)"', html)[1]
                js = (out / app).read_text(); self.assertNotIn("'data.json'", js); data = re.search(r"'(data\.[0-9a-f]{16}\.json)'", js)[1]
                for name in (app, css, data, '.nojekyll'):
                    self.assertTrue((out / name).exists(), name)
                return app, css, data
            first = build(); self.assertEqual(first, build())
            (src / 'data.json').write_text('{"version":2}'); second = build()
            self.assertNotEqual(first[0], second[0]); self.assertEqual(first[1], second[1]); self.assertNotEqual(first[2], second[2])
            (src / 'data.json').unlink()
            proc = subprocess.run([sys.executable, str(ROOT / 'scripts/build_static_release.py'), '--source', str(src), '--output', str(pathlib.Path(tmp) / 'out2')], capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0); self.assertFalse((pathlib.Path(tmp) / 'out2/index.html').exists())


if __name__ == '__main__':
    unittest.main()
