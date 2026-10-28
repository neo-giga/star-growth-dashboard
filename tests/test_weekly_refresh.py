import copy,importlib.util,json,pathlib,tempfile,unittest
from unittest.mock import patch
from trending.feeds import FeedClient
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('weekly_refresh',ROOT/'scripts/weekly_refresh.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
def current():
 return {'generated_at':'2026-01-01T00:00:00Z','feeds':[{'provider':'gittrend','period':p,'source_as_of':None,'fetched_at_start':'2026-01-01T00:00:00Z','fetched_at_end':'2026-01-01T00:00:00Z','coverage':{'status':'partial'},'rows':[{'repo_id':1,'full_name':'o/r','stars':100,'gain':None,'language':None,'created_at':'2020-01-01T00:00:00Z','created_at_github_repo_id':99,'metadata':{'github_repo_id':99,'description':'cached','observed_at':'2025-12-01T00:00:00Z'}}]} for p in ('day','week','month')]}
def factory(name='o/r',rid=1):
 def make(**kwargs):
  return FeedClient(fetch=lambda url:{'data':[{'id':rid,'fullName':name,'stars':101,'starsToday':'0'}],'pagination':{'page':1,'limit':100,'total':1,'totalPages':1,'hasMore':False}})
 return make
class WeeklyRefreshTests(unittest.TestCase):
 def test_same_identity_carries_metadata_not_freshness(self):
  old=current();new=w.refresh_dashboard(old,client_factory=factory())
  for f in new['feeds']:
   r=f['rows'][0];self.assertEqual(r['metadata']['observed_at'],'2025-12-01T00:00:00Z');self.assertEqual(r['created_at'],'2020-01-01T00:00:00Z');self.assertEqual(r['gain'],0);self.assertIsNone(f['source_as_of']);self.assertIsNotNone(r['observation']['fetched_at'])
  self.assertEqual(old,current())
 def test_rename_or_changed_provider_id_does_not_carry(self):
  for fn in (factory(name='new/r'),factory(rid=2)):
   r=w.refresh_dashboard(current(),client_factory=fn)['feeds'][0]['rows'][0]
   self.assertIsNone(r['metadata']);self.assertIsNone(r['created_at'])
 def fixture(self,tmp):
  site=pathlib.Path(tmp)/'current';site.mkdir();(site/'index.html').write_text('<link href="style.css"><script src="app.js"></script>');(site/'app.js').write_text("fetch('data.json')");(site/'style.css').write_text('body{}');(site/'data.json').write_text(json.dumps(current()));(site/'private-note.txt').write_text('not publishable');return site
 def builder(self,data,stage):
  from trending.agent_feed import build_agent_exports
  return build_agent_exports(data,stage)
 def test_validate_current_stages_unchanged_timestamps_no_network(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=self.fixture(tmp);out=pathlib.Path(tmp)/'output';before=(site/'data.json').read_bytes()
   result=w.stage_site(site,out,'validate-current',client_factory=lambda **kw:self.fail('network'),export_builder=self.builder)
   self.assertFalse(result['new_retrieval']);self.assertEqual((site/'data.json').read_bytes(),before);data,_=w.read_current(out);self.assertEqual([f['fetched_at_end'] for f in data['feeds']],[f['fetched_at_end'] for f in current()['feeds']]);self.assertEqual([f['rows'][0]['stars'] for f in data['feeds']],[100,100,100]);self.assertFalse((out/'private-note.txt').exists())
 def test_refresh_fills_missing_creation_dates_before_publishing(self):
  def fill(data):
   for f in data['feeds']:
    for r in f['rows']:
     if r['created_at'] is None: r['created_at']='2024-01-02T03:04:05+00:00';r['created_at_github_repo_id']=7
   return {'requested':3,'filled':3,'batches':1,'failed_batches':0,'errors':[]}
  with tempfile.TemporaryDirectory() as tmp:
   site=self.fixture(tmp);out=pathlib.Path(tmp)/'output'
   result=w.stage_site(site,out,'refresh',client_factory=factory(name='new/r'),export_builder=self.builder,created_dates=fill)
   self.assertEqual(result['created_dates']['filled'],3);data,_=w.read_current(out)
   self.assertEqual([f['rows'][0]['created_at'] for f in data['feeds']],['2024-01-02T03:04:05+00:00']*3)
   result=w.stage_site(site,pathlib.Path(tmp)/'validated','validate-current',export_builder=self.builder,created_dates=lambda data:self.fail('network'))
   self.assertIsNone(result['created_dates'])
 def test_partial_inconsistent_failure_does_not_touch_last_good(self):
  for status in ('partial','inconsistent'):
   with tempfile.TemporaryDirectory() as tmp:
    site=self.fixture(tmp);out=pathlib.Path(tmp)/'output';before=(site/'data.json').read_bytes()
    with patch.object(w,'collect_feed',return_value={'coverage':{'status':status}}):
     with self.assertRaises(ValueError):w.stage_site(site,out,'refresh',export_builder=self.builder)
    self.assertFalse(out.exists());self.assertEqual((site/'data.json').read_bytes(),before)
 def test_count_mismatch_failclosed(self):
  bad={'coverage':{'status':'complete','reached_end':True,'unique_repos':1,'reported_total':2,'last_reported_total':2,'duplicates':0}}
  with patch.object(w,'collect_feed',return_value=bad):
   with self.assertRaises(ValueError):w.refresh_dashboard(current())
 def test_builder_error_no_partial_output(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=self.fixture(tmp);out=pathlib.Path(tmp)/'output'
   with self.assertRaises(RuntimeError):w.stage_site(site,out,'validate-current',export_builder=lambda *a:(_ for _ in ()).throw(RuntimeError('export error')))
   self.assertFalse(out.exists());self.assertTrue((site/'index.html').exists())
 def test_unsafe_reference_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=self.fixture(tmp);(site/'index.html').write_text('<link href="style.css"><script src="../app.js"></script>')
   with self.assertRaises(ValueError):w.read_current(site)
 def test_existing_output_not_overwritten(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=self.fixture(tmp)
   with self.assertRaises(ValueError):w.stage_site(site,site,'validate-current')

 def test_compact_roundtrip_preserves_snapshot_and_cached_metadata(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=self.fixture(tmp);first=pathlib.Path(tmp)/'first';second=pathlib.Path(tmp)/'second'
   w.stage_site(site,first,'validate-current')
   firstdata,_=w.read_current(first)
   w.stage_site(first,second,'validate-current')
   seconddata,_=w.read_current(second)
   self.assertEqual(firstdata['agent_snapshot_id'],seconddata['agent_snapshot_id'])
   from trending.agent_feed import hydrate_ui_index
   full=hydrate_ui_index(seconddata,second)
   self.assertEqual(full['feeds'][0]['rows'][0]['metadata']['observed_at'],'2025-12-01T00:00:00Z')
