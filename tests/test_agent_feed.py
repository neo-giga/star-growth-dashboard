import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse
from trending.agent_feed import build_agent_exports, build_ui_files


def sample(n=2):
    return {'generated_at': '2026-10-26T06:00:00Z', 'feeds': [{'provider': 'gittrend', 'period': 'week',
        'source_as_of': None, 'fetched_at_start': '2026-10-26T05:00:00Z', 'fetched_at_end': '2026-10-26T06:00:00Z',
        'coverage': {'status': 'inconsistent', 'warnings': ['total changed']},
        'rows': [{'repo_id': i+1, 'full_name': f'owner/repo{i}', 'stars': 100+i, 'gain': i,
                  'language': 'Python', 'created_at': None, 'created_at_github_repo_id': None,
                  'observation': {'page': 1, 'source_url': 'https://gittrend.io/api/trending?time=week&page=1&limit=100', 'fetched_at': '2026-10-26T05:00:00Z'}} for i in range(n)]}]}


class AgentFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
    def path(self, url):
        return self.root / urlparse(url).path.removeprefix('/star-growth-dashboard/')
    def load(self, url):
        return json.loads(self.path(url).read_text())
    def test_manifest_hashes_links_nulls_and_provenance(self):
        data = sample()
        data['feeds'][0]['rows'][0]['gain'] = None
        catalog = build_agent_exports(data, self.root)
        manifest = self.load(catalog['manifest_url'])
        for f in manifest['files']:
            b = self.path(f['url']).read_bytes()
            self.assertEqual(len(b), f['bytes'])
            self.assertEqual(hashlib.sha256(b).hexdigest(), f['sha256'])
        p = self.load(catalog['feeds'][0]['first_page_url'])
        self.assertEqual(p['coverage']['status'], 'inconsistent')
        self.assertIsNone(p['source_as_of'])
        r = p['rows'][-1]
        self.assertIsNone(r['metrics']['star_growth']['value'])
        self.assertIsNone(r['metrics']['star_growth']['window_start'])
        self.assertIsNone(r['metadata']['topics'])
        self.assertIsNone(r['metadata']['contributor_count'])
        self.assertIn('contributor_count', r['provenance']['unknown_fields'])
        self.assertEqual(r['provenance']['fetched_at'], '2026-10-26T05:00:00Z')
        self.assertEqual(r['identity']['identity_match'], 'unverified')
    def test_pagination(self):
        c = build_agent_exports(sample(205), self.root)
        u, rows, sizes = c['feeds'][0]['first_page_url'], [], []
        while u:
            p = self.load(u); rows += p['rows']; sizes.append(len(p['rows'])); u = p['next_url']
        self.assertEqual(sizes, [100, 100, 5])
        self.assertEqual(len({r['record_id'] for r in rows}),205)
        self.assertEqual(len(self.load(c['feeds'][0]['top100_url'])['rows']),100)
    def test_deterministic_and_preserves_old(self):
        data = sample()
        c1 = build_agent_exports(data,self.root)
        self.assertEqual(c1,build_agent_exports(data,self.root))
        data['feeds'][0]['rows'][0]['stars'] += 1
        c2 = build_agent_exports(data,self.root)
        self.assertNotEqual(c1['snapshot_id'],c2['snapshot_id'])
        self.assertTrue(self.path(c1['manifest_url']).exists())
        self.assertEqual(json.loads((self.root/'api/v1/catalog.json').read_text()),c2)
    def test_immutable_corruption_rejected(self):
        data = sample(); c = build_agent_exports(data,self.root)
        self.path(c['feeds'][0]['top100_url']).write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'immutable snapshot'):
            build_agent_exports(data,self.root)
    def test_duplicate_rejected_before_catalog(self):
        d=sample(); d['feeds'][0]['rows'][1]['repo_id']=1
        with self.assertRaisesRegex(ValueError,'duplicate'):
            build_agent_exports(d,self.root)
        self.assertFalse((self.root/'api/v1/catalog.json').exists())
    def test_csv_formula_and_unknown_empty(self):
        d=sample(1); d['feeds'][0]['rows'][0]['metadata']={'description':' =HYPERLINK("evil")','topics':[]}
        c=build_agent_exports(d,self.root)
        rows=list(csv.DictReader(io.StringIO(self.path(c['feeds'][0]['csv_url']).read_text())))
        self.assertTrue(rows[0]['description'].startswith("'"))
        self.assertEqual(rows[0]['contributor_count'],'')
        self.assertEqual(rows[0]['topics'],'[]')
    def test_allowlist_excludes_extra(self):
        d=sample(1); d['feeds'][0]['rows'][0]['token']='synthetic_private_marker'
        c=build_agent_exports(d,self.root)
        self.assertNotIn('synthetic_private_marker',self.path(c['feeds'][0]['top100_url']).read_text())
    def test_negative_gain_is_preserved(self):
        d=sample(1);d['feeds'][0]['rows'][0]['gain']=-2
        c=build_agent_exports(d,self.root)
        self.assertEqual(self.load(c['feeds'][0]['top100_url'])['rows'][0]['metrics']['star_growth']['value'],-2)
    def test_invalid_metadata_rejected(self):
        for metadata in ({'topics':'abc'},{'archived':'false'},{'forks':-1},{'contributor_count':True}):
            d=sample(1);d['feeds'][0]['rows'][0]['metadata']=metadata
            with self.subTest(metadata=metadata),self.assertRaises(ValueError):
                build_agent_exports(d,self.root)
    def test_timestamp_and_window_validation(self):
        d=sample(1);d['feeds'][0]['rows'][0]['observation']['fetched_at']='2026-10-26T05:00:00'
        with self.assertRaisesRegex(ValueError,'timezone'):
            build_agent_exports(d,self.root)
        d=sample(1);d['feeds'][0]['fetched_at_start']='2026-10-27T05:00:00Z'
        with self.assertRaisesRegex(ValueError,'reversed'):
            build_agent_exports(d,self.root)
    def test_provider_id_not_github_identity(self):
        d=sample(1);d['feeds'][0]['rows'][0]['created_at_github_repo_id']=999
        c=build_agent_exports(d,self.root)
        r=self.load(c['feeds'][0]['top100_url'])['rows'][0]
        self.assertEqual(r['identity']['provider_repo_id'],1)
        self.assertEqual(r['identity']['github_metadata_repo_id'],999)
        self.assertEqual(r['identity']['identity_match'],'case_insensitive_name_only')
    def test_window_files_hold_only_rendered_fields_in_gain_order(self):
        d=sample(105)
        d['feeds'][0]['rows'][0]['metadata']={'description':'hello','topics':[], 'github_repo_id':99, 'contributor_count':4, 'contributor_measured_at':'2026-10-25T00:00:00Z'}
        d['feeds'][0]['metadata_coverage']={'rights':'not blanket CC0'}
        c=build_agent_exports(d,self.root)
        files=build_ui_files(d,c)
        self.assertEqual(list(files),['week'])
        week=files['week']
        self.assertEqual(week['agent_snapshot_id'],c['snapshot_id']);self.assertEqual(week['agent_catalog_url'],'https://neo-giga.github.io/star-growth-dashboard/api/v1/catalog.json');self.assertEqual(week['generated_at'],d['generated_at'])
        self.assertEqual(len(week['feeds']),1);feed=week['feeds'][0];self.assertEqual(feed['period'],'week');self.assertEqual(feed['metadata_coverage'],{'rights':'not blanket CC0'})
        self.assertEqual([r['repo_id'] for r in feed['rows']][:3],[105,104,103])
        for r in feed['rows']:
            self.assertEqual(sorted(r),['created_at','created_at_github_repo_id','full_name','gain','language','repo_id','stars'])
        self.assertEqual(d['feeds'][0]['rows'][0]['repo_id'],1)  # input untouched
    def test_window_files_are_independent_of_the_snapshot_pages(self):
        d=sample(3);c=build_agent_exports(d,self.root)
        self.assertNotIn('metadata_url',json.dumps(build_ui_files(d,c)))
    def test_conflicting_github_ids(self):
        d=sample(1);r=d['feeds'][0]['rows'][0];r['created_at_github_repo_id']=3;r['metadata']={'github_repo_id':4}
        with self.assertRaisesRegex(ValueError,'conflicting'):
            build_agent_exports(d,self.root)
    def test_markdown_slug_rejected(self):
        for slug in ('owner/[click](https:evil)', '<script>/repo', 'owner/repo|bad', 'owner/☃'):
            d=sample(1);d['feeds'][0]['rows'][0]['full_name']=slug
            with self.subTest(slug=slug),self.assertRaisesRegex(ValueError,'full_name'):
                build_agent_exports(d,self.root)
    def test_schema_and_export_version_part_of_identity(self):
        from unittest.mock import patch
        from trending import agent_feed
        d=sample(1);c=build_agent_exports(d,self.root)
        with patch.object(agent_feed,'EXPORT_FORMAT','test-new-render'):
            changed=build_agent_exports(d,self.root)
        self.assertNotEqual(c['snapshot_id'],changed['snapshot_id'])
        changed=build_agent_exports(d,self.root,base_url='https://example.test/data/')
        self.assertNotEqual(c['snapshot_id'],changed['snapshot_id'])
    def test_schema_strict_nested_fields(self):
        c=build_agent_exports(sample(1),self.root)
        schema=self.load(c['schema_url'])
        for key in ('identity','metadata','metrics','provenance'):
            node=schema['properties'][key]
            self.assertFalse(node['additionalProperties'])
            self.assertEqual(set(node['required']),set(node['properties']))
        self.assertEqual(schema['properties']['identity']['properties']['provider'],{'const':'gittrend'})
        self.assertEqual(schema['properties']['provenance']['properties']['fetched_at']['format'],'date-time')
    def test_empty_feed_page(self):
        c=build_agent_exports(sample(0),self.root)
        p=self.load(c['feeds'][0]['first_page_url'])
        self.assertEqual(p['rows'],[]);self.assertIsNone(p['next_url'])

if __name__ == '__main__':
    unittest.main()
