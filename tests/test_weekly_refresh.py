import copy,importlib.util,json,pathlib,subprocess,sys,tempfile,unittest,urllib.parse
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
  old=current();new=w.refresh_dashboard(old,client_factory=factory(),sleeper=lambda s:None)
  for f in new['feeds']:
   r=f['rows'][0];self.assertEqual(r['metadata']['observed_at'],'2025-12-01T00:00:00Z');self.assertEqual(r['created_at'],'2020-01-01T00:00:00Z');self.assertEqual(r['gain'],0);self.assertIsNone(f['source_as_of']);self.assertIsNotNone(r['observation']['fetched_at'])
  self.assertEqual(old,current())
 def test_rename_or_changed_provider_id_does_not_carry(self):
  for fn in (factory(name='new/r'),factory(rid=2)):
   r=w.refresh_dashboard(current(),client_factory=fn,sleeper=lambda s:None)['feeds'][0]['rows'][0]
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
   with patch.object(w.time,'sleep',lambda s:None):result=w.stage_site(site,out,'refresh',client_factory=factory(name='new/r'),export_builder=self.builder,created_dates=fill)
   self.assertEqual(result['created_dates']['filled'],3);data,_=w.read_current(out)
   self.assertEqual([f['rows'][0]['created_at'] for f in data['feeds']],['2024-01-02T03:04:05+00:00']*3)
   result=w.stage_site(site,pathlib.Path(tmp)/'validated','validate-current',export_builder=self.builder,created_dates=lambda data:self.fail('network'))
   self.assertIsNone(result['created_dates'])
 def test_partial_inconsistent_failure_does_not_touch_last_good(self):
  for status in ('partial','inconsistent'):
   with tempfile.TemporaryDirectory() as tmp:
    site=self.fixture(tmp);out=pathlib.Path(tmp)/'output';before=(site/'data.json').read_bytes()
    with patch.object(w,'collect_feed',return_value={'coverage':{'status':status}}):
     with self.assertRaises(ValueError):w.stage_site(site,out,'refresh',export_builder=self.builder,top=0)
    self.assertFalse(out.exists());self.assertEqual((site/'data.json').read_bytes(),before)
 def test_count_mismatch_failclosed(self):
  bad={'coverage':{'status':'complete','reached_end':True,'unique_repos':1,'reported_total':2,'last_reported_total':2,'duplicates':0}}
  with patch.object(w,'collect_feed',return_value=bad):
   with self.assertRaises(ValueError):w.refresh_dashboard(current(),top=0)
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


def prior(n):
 """Published data with n rows per period; ids and names line up with Provider so creation dates can carry."""
 rows=[{'repo_id':1000+i,'full_name':f'owner{i}/repo{i}','stars':5,'gain':1,'language':'Python','created_at':'2020-01-01T00:00:00Z','created_at_github_repo_id':None,'metadata':None} for i in range(n)]
 return {'generated_at':'2026-01-01T00:00:00Z','feeds':[{'provider':'gittrend','period':p,'source_as_of':None,'fetched_at_start':'2026-01-01T00:00:00Z','fetched_at_end':'2026-01-01T00:00:00Z','coverage':{'status':'partial'},'rows':copy.deepcopy(rows)} for p in ('day','week','month')]}
def site_fixture(tmp,data):
 site=pathlib.Path(tmp)/'current';site.mkdir();(site/'index.html').write_text('<link href="style.css"><script src="app.js"></script>');(site/'app.js').write_text("fetch('data.json')");(site/'style.css').write_text('body{}');(site/'data.json').write_text(json.dumps(data));return site
class Provider:
 """GitTrend /api/trending pages in the shape of the 2026-10-28 run checkpoints (100 rows, pagination with total/totalPages/hasMore).

 drift=(page,total) changes the reported total from that page on; duplicate_page repeats page 1's first id; short_page drops one row.
 Faults apply to the first fault_attempts reads of a period (attempts are counted by page-1 requests). Stars rise by one per
 attempt so a test can tell which pass was published. drop_after=(period,page) removes the rank-1 repository from the ranking
 once that page has been served, shifting every later page up by one rank; shift_every_pass shifts by one more rank per attempt."""
 def __init__(self,total,drift=None,duplicate_page=None,short_page=None,fault_attempts=99,drop_after=None,shift_every_pass=False):
  self.total=total;self.drift=drift;self.duplicate_page=duplicate_page;self.short_page=short_page;self.fault_attempts=fault_attempts;self.drop_after=drop_after;self.shift_every_pass=shift_every_pass;self.dropped=False;self.pages=[]
 def __call__(self,url):
  q=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query));page,limit=int(q['page']),int(q['limit']);self.pages.append((q['time'],page))
  attempt=sum(1 for t,n in self.pages if t==q['time'] and n==1)
  total=self.drift[1] if self.drift and page>=self.drift[0] else self.total
  offset=(1 if self.dropped else 0)+(attempt-1 if self.shift_every_pass else 0)
  rows=[{'id':1000+i,'fullName':f'owner{i}/repo{i}','stars':10**6-i+attempt,'starsToday':str(10**5-i),'language':'Python','createdAt':'2025-12-16T11:05:49.000Z','forks':1,'topics':[],'trendingScore':1.5} for i in range((page-1)*limit+offset,min(page*limit,total)+offset)]
  if attempt<=self.fault_attempts:
   if page==self.duplicate_page and rows:rows[0]=dict(rows[0],id=1000)
   if page==self.short_page:rows=rows[:-1]
  if self.drop_after==(q['time'],page):self.dropped=True
  count=-(-total//limit)
  return {'data':rows,'pagination':{'page':page,'limit':limit,'total':total,'totalPages':count,'hasMore':page<count}}
 def factory(self,**kwargs):return FeedClient(fetch=self,**kwargs)
class BoundedRefreshTests(unittest.TestCase):
 def test_bounded_success_reads_the_top_pages_twice_and_publishes_the_later_pass(self):
  p=Provider(12195);sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,sleeper=sleeps.append)
  for f in data['feeds']:
   self.assertEqual(len(f['rows']),2000);self.assertEqual(f['coverage']['bounded_top'],2000);self.assertEqual(f['coverage']['verified_passes'],['A','B']);self.assertEqual(f['coverage']['pages_fetched'],20);self.assertFalse(f['coverage']['reached_end']);self.assertEqual(f['coverage']['duplicates'],0)
   self.assertEqual(f['rows'][0]['stars'],10**6+2)  # pass B's rows, not pass A's
  self.assertEqual(len(p.pages),120);self.assertEqual(max(n for _,n in p.pages),20);self.assertEqual(sleeps,[330])
  day=data['feeds'][0];self.assertEqual(day['rows'][0]['created_at'],'2020-01-01T00:00:00Z');self.assertIsNone(day['rows'][1999]['created_at']);self.assertEqual(day['rows'][1999]['observation']['page'],20)
  self.assertEqual(len(w.refresh_dashboard(prior(300),client_factory=Provider(12195).factory,top=300,sleeper=sleeps.append)['feeds'][0]['rows']),300)
 def test_rank_shift_between_pages_is_caught_by_the_second_pass(self):
  # Codex review round 1: after page 1 the rank-1 repository drops out, so pages 2..20 return old ranks 102..2001: every page
  # full, 2000 unique ids, yet old rank 101 (id 1100) is missing and the list was never the provider's top 2,000.
  p=Provider(12195,drop_after=('day',1));sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,sleeper=sleeps.append)
  day=data['feeds'][0];ids=[r['repo_id'] for r in day['rows']]
  self.assertIn(1100,ids);self.assertNotIn(1000,ids);self.assertEqual(ids,list(range(1001,3001)))
  self.assertEqual(day['coverage']['verified_passes'],['B','C']);self.assertEqual([f['coverage']['verified_passes'] for f in data['feeds'][1:]],[['A','B']]*2)
  self.assertEqual(sleeps,[330,330]);self.assertEqual(len(p.pages),140)  # A and B for all periods, C for day only
 def test_passes_that_keep_disagreeing_fail_closed(self):
  p=Provider(12195,shift_every_pass=True);sleeps=[]
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(300));out=pathlib.Path(tmp)/'output';cp=pathlib.Path(tmp)/'cp';before=(site/'data.json').read_bytes()
   with patch.object(w.time,'sleep',sleeps.append):
    with self.assertRaisesRegex(ValueError,'day refresh not publishable: membership or order changed between passes; current site unchanged'):
     w.stage_site(site,out,'refresh',client_factory=p.factory,checkpoint_dir=cp,created_dates=lambda data:self.fail('network'))
   self.assertEqual(sleeps,[330,330]);self.assertEqual(len(p.pages),180);self.assertFalse(out.exists());self.assertEqual((site/'data.json').read_bytes(),before)
   self.assertEqual(sorted(d.name for d in cp.iterdir()),['day','day-retry','day-verify','month','month-retry','month-verify','week','week-retry','week-verify'])
 def test_total_drift_is_tolerated_in_bounded_mode_but_not_strict(self):
  p=Provider(66453,drift=(19,66574));sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,sleeper=sleeps.append)
  f=data['feeds'][0];self.assertEqual(len(f['rows']),2000);self.assertEqual((f['coverage']['reported_total'],f['coverage']['last_reported_total']),(66453,66574))
  self.assertIn('provider total changed from 66453 to 66574 at page 19',f['coverage']['warnings']);self.assertEqual(sleeps,[330])
  strict=Provider(250,drift=(2,260));strict_sleeps=[]
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: inconsistent; current site unchanged'):w.refresh_dashboard(prior(250),client_factory=strict.factory,top=0,sleeper=strict_sleeps.append)
  self.assertEqual(strict.pages,[('day',1),('day',2),('day',3)]);self.assertEqual(strict_sleeps,[])
 def test_duplicate_ids_retry_once_then_fail_closed(self):
  p=Provider(12195,duplicate_page=2);sleeps=[]
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(300));out=pathlib.Path(tmp)/'output';cp=pathlib.Path(tmp)/'cp';before=(site/'data.json').read_bytes()
   with patch.object(w.time,'sleep',sleeps.append):
    with self.assertRaisesRegex(ValueError,'day refresh not publishable: earlier pass: 1 duplicate provider IDs; current site unchanged'):
     w.stage_site(site,out,'refresh',client_factory=p.factory,checkpoint_dir=cp,created_dates=lambda data:self.fail('network'))
   self.assertEqual(sleeps,[330,330]);self.assertEqual(len(p.pages),180);self.assertEqual(max(n for _,n in p.pages),20)
   self.assertFalse(out.exists());self.assertEqual((site/'data.json').read_bytes(),before)
   for d in ('day','day-verify','day-retry'):self.assertTrue((cp/d/'day-100-000020.json').exists())
 def test_transient_fault_recovers_on_the_single_retry(self):
  p=Provider(12195,duplicate_page=2,fault_attempts=1);sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,sleeper=sleeps.append)
  self.assertEqual(sleeps,[330,330]);self.assertEqual(len(p.pages),180);self.assertEqual([len(f['rows']) for f in data['feeds']],[2000]*3)
  self.assertEqual([f['coverage']['verified_passes'] for f in data['feeds']],[['B','C']]*3);self.assertEqual(data['feeds'][0]['rows'][0]['stars'],10**6+3)
 def test_short_non_last_page_fails_closed(self):
  p=Provider(12195,short_page=5);sleeps=[]
  with self.assertRaisesRegex(ValueError,'page 5 returned 99, expected 100'):w.refresh_dashboard(prior(300),client_factory=p.factory,sleeper=sleeps.append)
  self.assertEqual(sleeps,[330,330])
 def test_shrink_guard_refuses_a_collapsed_feed(self):
  sleeps=[]
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: 10 rows is under half of min\\(top, previous 300\\)'):w.refresh_dashboard(prior(300),client_factory=Provider(10).factory,sleeper=sleeps.append)
  self.assertEqual(sleeps,[330])
  self.assertEqual(len(w.refresh_dashboard(prior(300),client_factory=Provider(150).factory,sleeper=sleeps.append)['feeds'][0]['rows']),150)
  with self.assertRaisesRegex(ValueError,'999 rows is under half of min\\(top, previous 2000\\)'):w.refresh_dashboard(prior(2000),client_factory=Provider(999).factory,sleeper=sleeps.append)
 def test_provider_with_fewer_rows_than_top_reaches_its_end(self):
  p=Provider(1250);sleeps=[];data=w.refresh_dashboard(prior(1000),client_factory=p.factory,sleeper=sleeps.append)
  f=data['feeds'][0];self.assertEqual(len(f['rows']),1250);self.assertTrue(f['coverage']['reached_end']);self.assertEqual(f['coverage']['pages_fetched'],13);self.assertEqual(max(n for _,n in p.pages),13);self.assertEqual(sleeps,[330])
 def test_top_validation(self):
  network=lambda **kw:self.fail('network')
  for top in (150,50,10100,-100,'2000',2000.0,None,True):
   with self.assertRaisesRegex(ValueError,'top'):w.refresh_dashboard(prior(1),client_factory=network,top=top)
  with self.assertRaisesRegex(ValueError,'max_pages'):w.refresh_dashboard(prior(1),client_factory=network,top=2000,max_pages=19)
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(1));out=pathlib.Path(tmp)/'output'
   proc=subprocess.run([sys.executable,str(ROOT/'scripts/weekly_refresh.py'),'--site-dir',str(site),'--output-dir',str(out),'--top','150'],capture_output=True,text=True)
   self.assertEqual(proc.returncode,2);result=json.loads(proc.stdout);self.assertIn('top',result['error']);self.assertTrue(result['current_site_preserved']);self.assertFalse(out.exists())
