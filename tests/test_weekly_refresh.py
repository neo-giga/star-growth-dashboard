import copy,importlib.util,json,pathlib,subprocess,sys,tempfile,unittest,urllib.error,urllib.parse
from unittest.mock import patch
from trending.feeds import FeedClient
from trending.published import LiveSite,LocalSite,read_snapshot,snapshot_records
from trending.agent_feed import build_agent_exports,build_ui_files
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('weekly_refresh',ROOT/'scripts/weekly_refresh.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
INDEX_SOURCE='<link rel="stylesheet" href="style.css"><script src="app.js" defer></script>'
APP_SOURCE="const DATA_FILES = {day: 'data-day.json', week: 'data-week.json', month: 'data-month.json'};"
NO_SITE='http://127.0.0.1:9/'
def current():
 return {'generated_at':'2026-01-01T00:00:00Z','feeds':[{'provider':'gittrend','period':p,'source_as_of':None,'fetched_at_start':'2026-01-01T00:00:00Z','fetched_at_end':'2026-01-01T00:00:00Z','coverage':{'status':'partial'},'rows':[{'repo_id':1,'full_name':'o/r','stars':100,'gain':None,'language':None,'created_at':'2020-01-01T00:00:00Z','created_at_github_repo_id':99,'metadata':{'github_repo_id':99,'description':'cached','observed_at':'2025-12-01T00:00:00Z'}}]} for p in ('day','week','month')]}
def factory(name='o/r',rid=1):
 def make(**kwargs):
  return FeedClient(fetch=lambda url:{'data':[{'id':rid,'fullName':name,'stars':101,'starsToday':'0'}],'pagination':{'page':1,'limit':100,'total':1,'totalPages':1,'hasMore':False}})
 return make
def checkout(tmp,*data_files,name='checkout'):
 """A checkout: the UI sources, the hand-written public files, a private file, and optional historical data files (name, data)."""
 site=pathlib.Path(tmp)/name;site.mkdir();(site/'index.html').write_text(INDEX_SOURCE);(site/'app.js').write_text(APP_SOURCE);(site/'style.css').write_text('body{}')
 (site/'.nojekyll').touch();(site/'llms.txt').write_text('llms');(site/'docs').mkdir();(site/'docs'/'agent-feed.md').write_text('docs');(site/'private-note.txt').write_text('not publishable')
 for file_name,data in data_files:(site/file_name).write_text(json.dumps(data))
 return site
def publish(tmp,data,name='published'):
 """A site tree as a run publishes it (agent snapshot, per-window hashed data files, hashed app), built with the real tools."""
 root=pathlib.Path(tmp)/name;root.mkdir();catalog=build_agent_exports(data,root);src=pathlib.Path(tmp)/(name+'-src');src.mkdir()
 (src/'index.html').write_text(INDEX_SOURCE);(src/'app.js').write_text(APP_SOURCE);(src/'style.css').write_text('body{}')
 for period,payload in build_ui_files(data,catalog).items():(src/f'data-{period}.json').write_text(json.dumps(payload))
 subprocess.run([sys.executable,str(ROOT/'scripts/build_static_release.py'),'--source',str(src),'--output',str(root)],check=True,capture_output=True);return root
def live(root):
 """A published tree served as the live site; fetches are counted on the returned LiveSite."""
 base='https://example.test/site/';return LiveSite(base,fetch=lambda url:(pathlib.Path(root)/url.removeprefix(base)).read_bytes())
def offline():
 def fetch(url):raise urllib.error.URLError('synthetic: no route to the live site')
 return LiveSite('https://example.test/site/',fetch=fetch)
def listing(root):
 return sorted(str(p.relative_to(root)) for p in pathlib.Path(root).rglob('*') if p.is_file())
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
 def test_validate_current_re_releases_the_previous_data_and_snapshot_verbatim(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp);pub=publish(tmp,current());out=pathlib.Path(tmp)/'output';served=live(pub)
   result=w.stage_site(site,out,'validate-current',client_factory=lambda **kw:self.fail('network'),published=served)
   self.assertFalse(result['new_retrieval']);self.assertEqual(result['previous_published'],'https://example.test/site/');self.assertIsNone(result['created_dates'])
   data=w.read_current(out);self.assertEqual([f['fetched_at_end'] for f in data['feeds']],['2026-01-01T00:00:00Z']*3);self.assertEqual([f['rows'][0]['stars'] for f in data['feeds']],[100,100,100])
   self.assertEqual(data['agent_snapshot_id'],w.read_current(pub)['agent_snapshot_id']);self.assertFalse((out/'private-note.txt').exists())
   snapshot=[p for p in listing(pub) if p.startswith('api/')];self.assertEqual([p for p in listing(out) if p.startswith('api/')],snapshot)
   for name in snapshot:self.assertEqual((out/name).read_bytes(),(pub/name).read_bytes(),name)
   self.assertEqual(served.requests,2+3+3+len([p for p in snapshot if '/snapshots/' in p and not p.endswith('schema.json')])+6)  # index, app, 3 windows, catalog+manifest+schema, snapshot files; previous generation: index, app, style, 3 windows
   self.assertEqual(sorted(p for p in listing(out) if not p.startswith(('api/','app.','data-','style.'))),['.nojekyll','docs/agent-feed.md','index.html','llms.txt'])
 def test_validate_current_fails_closed_without_the_previous_publication(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp,('data.json',current()));out=pathlib.Path(tmp)/'output'
   with self.assertRaisesRegex(ValueError,'previous publication unreadable from https://example.test/site/: URLError: .*no route to the live site.*; current site unchanged'):
    w.stage_site(site,out,'validate-current',published=offline())
   self.assertFalse(out.exists())
   pub=publish(tmp,current());(pub/'api/v1/catalog.json').write_text('{"snapshot_id":"0000"}')
   with self.assertRaisesRegex(ValueError,'validate-current needs the published snapshot; published snapshot .* unreadable'):w.stage_site(site,out,'validate-current',published=live(pub))
   self.assertFalse(out.exists())
 def test_refresh_fills_missing_creation_dates_before_publishing(self):
  def fill(data):
   for f in data['feeds']:
    for r in f['rows']:
     if r['created_at'] is None: r['created_at']='2024-01-02T03:04:05+00:00';r['created_at_github_repo_id']=7
   return {'requested':3,'filled':3,'batches':1,'failed_batches':0,'errors':[]}
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp);pub=publish(tmp,current());out=pathlib.Path(tmp)/'output'
   with patch.object(w.time,'sleep',lambda s:None):result=w.stage_site(site,out,'refresh',client_factory=factory(name='new/r'),created_dates=fill,published=live(pub))
   self.assertEqual(result['created_dates']['filled'],3);data=w.read_current(out);self.assertEqual([p['created_at_known'] for p in result['periods']],[1,1,1])
   self.assertEqual([f['rows'][0]['created_at'] for f in data['feeds']],['2024-01-02T03:04:05+00:00']*3)
   result=w.stage_site(site,pathlib.Path(tmp)/'validated','validate-current',created_dates=lambda data:self.fail('network'),published=LocalSite(out))
   self.assertIsNone(result['created_dates']);self.assertEqual(result['snapshot_id'],w.read_current(out)['agent_snapshot_id'])
 def test_refresh_fails_closed_without_the_previous_publication_and_never_falls_back_to_the_checkout(self):
  # Codex full-lists review #3: the checkout's frozen data files lack every date learned since; publishing from them (with GitHub
  # lookups down) would turn carried creation dates back into nulls and feed the shrink guard stale counts.
  newest=prior(100,created='2021-01-01T00:00:00Z');newest['generated_at']='2026-10-28T00:00:00Z';older=prior(300);older['generated_at']='2026-10-20T00:00:00Z'
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp,('data.1111111111111111.json',older),('data.json',older));out=pathlib.Path(tmp)/'output'
   with patch.object(w.time,'sleep',lambda s:None),self.assertRaisesRegex(ValueError,'previous publication unreadable from https://example.test/site/: URLError: .*; current site unchanged'):
    w.stage_site(site,out,'refresh',client_factory=Provider(300).factory,created_dates=lambda data:{'requested':0},published=offline())
   self.assertFalse(out.exists())
   # Readable previous publication: its dates carry first, the checkout's older files only seed rows it does not hold.
   pub=publish(tmp,newest)
   with patch.object(w.time,'sleep',lambda s:None):result=w.stage_site(site,out,'refresh',client_factory=Provider(300).factory,created_dates=lambda data:{'requested':0},published=LocalSite(pub,label='artifact:run 7'))
   self.assertEqual(result['previous_published'],'artifact:run 7')
   rows=w.read_current(out)['feeds'][0]['rows'];by_id={r['repo_id']:r['created_at'] for r in rows}
   self.assertEqual((by_id[1000],by_id[1099],by_id[1100],by_id[1299]),('2021-01-01T00:00:00Z','2021-01-01T00:00:00Z','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z'));self.assertEqual(len(rows),300)
 def test_refresh_carries_cached_metadata_from_the_published_snapshot(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp);pub=publish(tmp,current());out=pathlib.Path(tmp)/'output';served=live(pub)
   with patch.object(w.time,'sleep',lambda s:None):result=w.stage_site(site,out,'refresh',client_factory=factory(),created_dates=lambda data:{'requested':0},published=served)
   self.assertIn('published snapshot records (cached GitHub metadata) attached to 3 previously published rows',result['notes']);self.assertEqual(served.requests,2+3+2+3+6)  # index, app, 3 windows, catalog, manifest, the snapshot's three all.jsonl files only; previous generation: index, app, style, 3 windows
   records=snapshot_records(read_snapshot(LocalSite(out),result['snapshot_id'],'records'))
   r=records['week']['gittrend:1'];self.assertEqual(r['metadata']['description'],'cached');self.assertEqual(r['provenance']['metadata_observed_at'],'2025-12-01T00:00:00Z');self.assertEqual(r['identity']['github_metadata_repo_id'],99)
   broken=publish(tmp,current(),name='broken');(broken/'api/v1/catalog.json').write_text('{"snapshot_id":"0000"}')
   with patch.object(w.time,'sleep',lambda s:None):result=w.stage_site(site,pathlib.Path(tmp)/'out2','refresh',client_factory=factory(),created_dates=lambda data:{'requested':0},published=live(broken))
   self.assertTrue(any(n.startswith('published snapshot') and 'unreadable' in n for n in result['notes']));self.assertIn('published snapshot records (cached GitHub metadata) attached to 0 previously published rows',result['notes'])
 def test_partial_inconsistent_failure_does_not_touch_last_good(self):
  for status in ('partial','inconsistent'):
   with tempfile.TemporaryDirectory() as tmp:
    site=checkout(tmp);pub=publish(tmp,current());out=pathlib.Path(tmp)/'output';before=listing(pub)
    with patch.object(w,'collect_feed',return_value={'coverage':{'status':status}}):
     with self.assertRaises(ValueError):w.stage_site(site,out,'refresh',top=0,published=live(pub))
    self.assertFalse(out.exists());self.assertEqual(listing(pub),before)
 def test_count_mismatch_failclosed(self):
  bad={'coverage':{'status':'complete','reached_end':True,'unique_repos':1,'reported_total':2,'last_reported_total':2,'duplicates':0}}
  with patch.object(w,'collect_feed',return_value=bad):
   with self.assertRaises(ValueError):w.refresh_dashboard(current(),top=0)
 def test_builder_error_no_partial_output(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp);pub=publish(tmp,current());out=pathlib.Path(tmp)/'output'
   with patch.object(w.time,'sleep',lambda s:None):
    with self.assertRaises(RuntimeError):w.stage_site(site,out,'refresh',client_factory=factory(),created_dates=lambda data:{'requested':0},export_builder=lambda *a:(_ for _ in ()).throw(RuntimeError('export error')),published=live(pub))
   self.assertFalse(out.exists());self.assertTrue((site/'index.html').exists())
 def test_unsafe_reference_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   pub=publish(tmp,current());(pub/'index.html').write_text('<link href="style.css"><script src="../app.js"></script>')
   with self.assertRaises(ValueError):w.read_current(pub)
   site=checkout(tmp);(site/'index.html').write_text('<link href="style.css"><script src="app.844cb05d8617d41a.js"></script>')
   with self.assertRaisesRegex(ValueError,'index.html must reference app.js and style.css'):w.stage_site(site,pathlib.Path(tmp)/'out','validate-current',published=live(pub))
 def test_existing_output_not_overwritten(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp)
   with self.assertRaises(ValueError):w.stage_site(site,site,'validate-current')
 def test_artifact_holds_only_the_latest_snapshot_the_current_assets_and_one_previous_generation(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp,('data.3d500242a0406175.json',prior(5)),('data.json',prior(5)));old=publish(tmp,prior(5),name='old');(site/'docs'/'weekly-refresh.yml').write_text('name: x')
   for name in listing(old):  # the checkout still holds yesterday's artifact (its index.html stays the source)
    if name!='index.html':(site/name).parent.mkdir(parents=True,exist_ok=True);(site/name).write_bytes((old/name).read_bytes())
   (site/'app.844cb05d8617d41a.js').write_text('old app');pub=publish(tmp,prior(5));out=pathlib.Path(tmp)/'output'
   with patch.object(w.time,'sleep',lambda s:None):result=w.stage_site(site,out,'refresh',client_factory=Provider(5).factory,created_dates=lambda data:{'requested':0},published=live(pub))
   files=listing(out);snapshots={p.split('/')[3] for p in files if p.startswith('api/v1/snapshots/')}
   self.assertEqual(snapshots,{result['snapshot_id']});self.assertNotEqual(result['snapshot_id'],w.read_current(old)['agent_snapshot_id'])
   self.assertEqual([p for p in files if not p.startswith(('api/','app.','data-','style.'))],['.nojekyll','docs/agent-feed.md','docs/weekly-refresh.yml','index.html','llms.txt'])
   current=w.read_site_data(LocalSite(out))[1];previous=w.read_site_data(LocalSite(pub))[1]
   self.assertEqual(sorted(p for p in files if p.startswith(('app.','data-','style.'))),sorted([current['app'],previous['app'],*current['data'].values(),*previous['data'].values()]+[p for p in listing(pub) if p.startswith('style.')]))
   self.assertEqual(result['previous_generation'],sorted([previous['app'],*previous['data'].values()]+[p for p in listing(pub) if p.startswith('style.')]))
   for name in result['previous_generation']:self.assertEqual((out/name).read_bytes(),(pub/name).read_bytes())  # yesterday's tab still finds its files
   self.assertNotIn('app.844cb05d8617d41a.js',files)  # the checkout's frozen assets are never published
   later=pathlib.Path(tmp)/'later'
   with patch.object(w.time,'sleep',lambda s:None):w.stage_site(site,later,'refresh',client_factory=Provider(6).factory,created_dates=lambda data:{'requested':0},published=LocalSite(out))
   older_only=set(result['previous_generation'])-{current['app'],*current['data'].values()}-{p for p in listing(out) if p.startswith('style.')}
   self.assertTrue(older_only);self.assertFalse(older_only&set(listing(later)))  # one generation only: the day before yesterday's own files are gone
   self.assertEqual(result['artifact_bytes'],sum((out/p).stat().st_size for p in files));self.assertEqual(sorted(result['data_files']),['day','month','week']);self.assertGreater(result['data_files']['week']['bytes'],result['data_files']['week']['gzip_bytes'])
 def test_agent_feed_exports_only_the_verified_head(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp);pub=publish(tmp,prior(5));out=pathlib.Path(tmp)/'output'
   with patch.object(w.time,'sleep',lambda s:None):result=w.stage_site(site,out,'refresh',client_factory=Provider(12195).factory,created_dates=lambda data:{'requested':0},verify_top=300,published=live(pub))
   data=w.read_current(out);catalog=json.loads((out/'api/v1/catalog.json').read_text())
   self.assertEqual([len(f['rows']) for f in data['feeds']],[12195]*3);self.assertEqual([f['total'] for f in catalog['feeds']],[300]*3);self.assertEqual([p['verified_rows'] for p in result['periods']],[300]*3)
   records=snapshot_records(read_snapshot(LocalSite(out),catalog['snapshot_id'],'records'));self.assertEqual(len(records['day']),300);self.assertEqual(catalog['feeds'][0]['coverage']['unique_repos'],12195)
   self.assertEqual(data['feeds'][0]['rows'][0]['repo_id'],1000);self.assertEqual(sorted(data['feeds'][0]['rows'][0]),sorted(['repo_id','full_name','stars','gain','language','created_at','created_at_github_repo_id']))


def prior(n,created='2020-01-01T00:00:00Z'):
 """Published data with n rows per period; ids and names line up with Provider so creation dates can carry."""
 rows=[{'repo_id':1000+i,'full_name':f'owner{i}/repo{i}','stars':5,'gain':1,'language':'Python','created_at':created,'created_at_github_repo_id':None,'metadata':None} for i in range(n)]
 return {'generated_at':'2026-01-01T00:00:00Z','feeds':[{'provider':'gittrend','period':p,'source_as_of':None,'fetched_at_start':'2026-01-01T00:00:00Z','fetched_at_end':'2026-01-01T00:00:00Z','coverage':{'status':'partial'},'rows':copy.deepcopy(rows)} for p in ('day','week','month')]}
def site_fixture(tmp,data):
 """A checkout plus the published site holding data; stage_site calls pass published=published_of(tmp)."""
 publish(tmp,data);return checkout(tmp,('data.json',data))
def published_of(tmp):return LocalSite(pathlib.Path(tmp)/'published')
class Provider:
 """GitTrend /api/trending pages in the shape of the 2026-10-28 run checkpoints (100 rows, pagination with total/totalPages/hasMore).

 drift=(page,total) changes the reported total from that page on; duplicate_page repeats page 1's first id; short_page drops one row.
 Faults apply to the first fault_attempts reads of a period (attempts are counted by page-1 requests). Stars rise by one per
 attempt so a test can tell which pass was published. drop_after=(period,page) removes the rank-1 repository from the ranking
 once that page has been served, shifting every later page up by one rank; shift_every_pass shifts by one more rank per attempt."""
 def __init__(self,total,drift=None,duplicate_page=None,short_page=None,fault_attempts=99,drop_after=None,shift_every_pass=False,fail_page=None,totals=None,short_pages=None):
  self.total=total;self.drift=drift;self.duplicate_page=duplicate_page;self.short_page=short_page;self.fault_attempts=fault_attempts;self.drop_after=drop_after;self.shift_every_pass=shift_every_pass;self.dropped=False;self.pages=[]
  self.fail_page=fail_page;self.totals=totals or {};self.short_pages=short_pages  # totals: per-period provider total; short_pages=(first page, rows): every page from there on loses that many rows
 def __call__(self,url):
  q=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query));page,limit=int(q['page']),int(q['limit']);self.pages.append((q['time'],page))
  if page==self.fail_page:raise urllib.error.URLError('synthetic connection reset')
  attempt=sum(1 for t,n in self.pages if t==q['time'] and n==1)
  base=self.totals.get(q['time'],self.total)
  total=self.drift[1] if self.drift and page>=self.drift[0] else base
  offset=(1 if self.dropped else 0)+(attempt-1 if self.shift_every_pass else 0)
  rows=[{'id':1000+i,'fullName':f'owner{i}/repo{i}','stars':10**6-i+attempt,'starsToday':str(10**5-i),'language':'Python','createdAt':'2025-12-16T11:05:49.000Z','forks':1,'topics':[],'trendingScore':1.5} for i in range((page-1)*limit+offset,min(page*limit,total)+offset)]
  if self.short_pages and page>=self.short_pages[0]:rows=rows[:len(rows)-self.short_pages[1]]
  if attempt<=self.fault_attempts:
   if page==self.duplicate_page and rows:rows[0]=dict(rows[0],id=1000)
   if page==self.short_page:rows=rows[:-1]
  if self.drop_after==(q['time'],page):self.dropped=True
  count=-(-total//limit)
  return {'data':rows,'pagination':{'page':page,'limit':limit,'total':total,'totalPages':count,'hasMore':page<count}}
 def factory(self,**kwargs):return FeedClient(fetch=self,**kwargs)
class ReplayingCache:
 """A shared cache in front of a Provider: a page served under window seconds ago (GitTrend's s-maxage 300 + stale-while-revalidate
 600) is replayed as-is. The clock advances one second per request (the pacing floor) and by whatever the refresh sleeps."""
 def __init__(self,provider,window=900):self.provider=provider;self.window=window;self.now=0;self.stored={};self.replayed=0
 def sleep(self,seconds):self.now+=seconds
 def __call__(self,url):
  self.now+=1;hit=self.stored.get(url)
  if hit and self.now-hit[0]<self.window:self.replayed+=1;return copy.deepcopy(hit[1])
  payload=self.provider(url);self.stored[url]=(self.now,payload);return copy.deepcopy(payload)
 def factory(self,**kwargs):return FeedClient(fetch=self,**kwargs)
class BoundedRefreshTests(unittest.TestCase):
 def test_bounded_success_reads_the_top_pages_twice_and_publishes_the_later_pass(self):
  p=Provider(12195);sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,top=2000,sleeper=sleeps.append)
  for f in data['feeds']:
   self.assertEqual(len(f['rows']),2000);self.assertEqual(f['coverage']['bounded_top'],2000);self.assertEqual(f['coverage']['verified_passes'],['A','B']);self.assertEqual(f['coverage']['pages_fetched'],20);self.assertFalse(f['coverage']['reached_end']);self.assertEqual(f['coverage']['duplicates'],0)
   self.assertEqual(f['rows'][0]['stars'],10**6+2)  # pass B's rows, not pass A's
  self.assertEqual(len(p.pages),120);self.assertEqual(max(n for _,n in p.pages),20);self.assertEqual(sleeps,[910])
  day=data['feeds'][0];self.assertEqual(day['rows'][0]['created_at'],'2020-01-01T00:00:00Z');self.assertIsNone(day['rows'][1999]['created_at']);self.assertEqual(day['rows'][1999]['observation']['page'],20)
  self.assertEqual(len(w.refresh_dashboard(prior(300),client_factory=Provider(12195).factory,top=300,sleeper=sleeps.append)['feeds'][0]['rows']),300)
 def test_rank_shift_between_pages_is_caught_by_the_second_pass(self):
  # Codex review round 1: after page 1 the rank-1 repository drops out, so pages 2..20 return old ranks 102..2001: every page
  # full, 2000 unique ids, yet old rank 101 (id 1100) is missing and the list was never the provider's top 2,000.
  p=Provider(12195,drop_after=('day',1));sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,top=2000,sleeper=sleeps.append)
  day=data['feeds'][0];ids=[r['repo_id'] for r in day['rows']]
  self.assertIn(1100,ids);self.assertNotIn(1000,ids);self.assertEqual(ids,list(range(1001,3001)))
  self.assertEqual(day['coverage']['verified_passes'],['B','C']);self.assertEqual([f['coverage']['verified_passes'] for f in data['feeds'][1:]],[['A','B']]*2)
  self.assertEqual(sleeps,[910,910]);self.assertEqual(len(p.pages),140)  # A and B for all periods, C for day only
 def test_cached_replay_cannot_pass_the_membership_check(self):
  # Codex review round 2: a shared cache honouring s-maxage=300, stale-while-revalidate=600 may replay pass A's mixed pages (page 1
  # before the rank-1 drop, pages 2..20 after) for up to 900 s. Pass B must start past that window so it reads fresh, coherent pages.
  cache=ReplayingCache(Provider(12195,drop_after=('day',1)));sleeps=[]
  def sleeper(seconds):sleeps.append(seconds);cache.sleep(seconds)
  data=w.refresh_dashboard(prior(300),client_factory=cache.factory,top=2000,sleeper=sleeper)
  day=data['feeds'][0];ids=[r['repo_id'] for r in day['rows']]
  self.assertIn(1100,ids);self.assertEqual(ids,list(range(1001,3001)))
  self.assertEqual(day['coverage']['verified_passes'],['B','C']);self.assertEqual(sleeps,[910,910]);self.assertEqual(cache.replayed,0);self.assertEqual(w.RETRY_DELAY,300+600+10)
 def test_passes_that_keep_disagreeing_fail_closed(self):
  p=Provider(12195,shift_every_pass=True);sleeps=[]
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(300));out=pathlib.Path(tmp)/'output';cp=pathlib.Path(tmp)/'cp';before=(site/'data.json').read_bytes()
   with patch.object(w.time,'sleep',sleeps.append):
    with self.assertRaisesRegex(ValueError,'day refresh not publishable: membership or order changed between passes; current site unchanged'):
     w.stage_site(site,out,'refresh',client_factory=p.factory,checkpoint_dir=cp,created_dates=lambda data:self.fail('network'),top=2000,published=published_of(tmp))
   self.assertEqual(sleeps,[910,910]);self.assertEqual(len(p.pages),180);self.assertFalse(out.exists());self.assertEqual((site/'data.json').read_bytes(),before)
   self.assertEqual(sorted(d.name for d in cp.iterdir()),['day','day-retry','day-verify','month','month-retry','month-verify','week','week-retry','week-verify'])
 def test_total_drift_is_tolerated_in_bounded_mode_but_not_strict(self):
  p=Provider(66453,drift=(19,66574));sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,top=2000,sleeper=sleeps.append)
  f=data['feeds'][0];self.assertEqual(len(f['rows']),2000);self.assertEqual((f['coverage']['reported_total'],f['coverage']['last_reported_total']),(66453,66574))
  self.assertIn('provider total changed from 66453 to 66574 at page 19',f['coverage']['warnings']);self.assertEqual(sleeps,[910])
  strict=Provider(250,drift=(2,260));strict_sleeps=[]
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: inconsistent; current site unchanged'):w.refresh_dashboard(prior(250),client_factory=strict.factory,top=0,sleeper=strict_sleeps.append)
  self.assertEqual(strict.pages,[('day',1),('day',2),('day',3)]);self.assertEqual(strict_sleeps,[])
 def test_duplicate_ids_retry_once_then_fail_closed(self):
  p=Provider(12195,duplicate_page=2);sleeps=[]
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(300));out=pathlib.Path(tmp)/'output';cp=pathlib.Path(tmp)/'cp';before=(site/'data.json').read_bytes()
   with patch.object(w.time,'sleep',sleeps.append):
    with self.assertRaisesRegex(ValueError,'day refresh not publishable: earlier pass: 1 duplicate provider IDs; current site unchanged'):
     w.stage_site(site,out,'refresh',client_factory=p.factory,checkpoint_dir=cp,created_dates=lambda data:self.fail('network'),top=2000,published=published_of(tmp))
   self.assertEqual(sleeps,[910,910]);self.assertEqual(len(p.pages),180);self.assertEqual(max(n for _,n in p.pages),20)
   self.assertFalse(out.exists());self.assertEqual((site/'data.json').read_bytes(),before)
   for d in ('day','day-verify','day-retry'):self.assertTrue((cp/d/'day-100-000020.json').exists())
 def test_transient_fault_recovers_on_the_single_retry(self):
  p=Provider(12195,duplicate_page=2,fault_attempts=1);sleeps=[];data=w.refresh_dashboard(prior(300),client_factory=p.factory,top=2000,sleeper=sleeps.append)
  self.assertEqual(sleeps,[910,910]);self.assertEqual(len(p.pages),180);self.assertEqual([len(f['rows']) for f in data['feeds']],[2000]*3)
  self.assertEqual([f['coverage']['verified_passes'] for f in data['feeds']],[['B','C']]*3);self.assertEqual(data['feeds'][0]['rows'][0]['stars'],10**6+3)
 def test_short_non_last_page_fails_closed(self):
  p=Provider(12195,short_page=5);sleeps=[]
  with self.assertRaisesRegex(ValueError,'page 5 returned 99, expected 100'):w.refresh_dashboard(prior(300),client_factory=p.factory,top=2000,sleeper=sleeps.append)
  self.assertEqual(sleeps,[910,910])
 def test_shrink_guard_refuses_a_collapsed_feed(self):
  sleeps=[]
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: 10 rows is under half of min\\(depth, previous 300\\)'):w.refresh_dashboard(prior(300),client_factory=Provider(10).factory,top=2000,sleeper=sleeps.append)
  self.assertEqual(sleeps,[910])
  self.assertEqual(len(w.refresh_dashboard(prior(300),client_factory=Provider(150).factory,top=2000,sleeper=sleeps.append)['feeds'][0]['rows']),150)
  with self.assertRaisesRegex(ValueError,'999 rows is under half of min\\(depth, previous 2000\\)'):w.refresh_dashboard(prior(2000),client_factory=Provider(999).factory,top=2000,sleeper=sleeps.append)
 def test_provider_with_fewer_rows_than_top_reaches_its_end(self):
  p=Provider(1250);sleeps=[];data=w.refresh_dashboard(prior(1000),client_factory=p.factory,top=2000,sleeper=sleeps.append)
  f=data['feeds'][0];self.assertEqual(len(f['rows']),1250);self.assertTrue(f['coverage']['reached_end']);self.assertEqual(f['coverage']['pages_fetched'],13);self.assertEqual(max(n for _,n in p.pages),13);self.assertEqual(sleeps,[910])
 def test_top_validation(self):
  network=lambda **kw:self.fail('network')
  for top in (150,50,10100,-100,'2000',2000.0,True):
   with self.assertRaisesRegex(ValueError,'top'):w.refresh_dashboard(prior(1),client_factory=network,top=top)
  with self.assertRaisesRegex(ValueError,'max_pages'):w.refresh_dashboard(prior(1),client_factory=network,top=2000,max_pages=19)
  for depth,verify in ((150,2000),('some',2000),(0,2000),(2000,150),(2000,0),(2000,'2000'),(2000,True)):
   with self.assertRaisesRegex(ValueError,'depth|verify_top'):w.refresh_dashboard(prior(1),client_factory=network,depth=depth,verify_top=verify)
  with self.assertRaisesRegex(ValueError,'max_pages'):w.refresh_dashboard(prior(1),client_factory=network,max_pages=19)
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(1));out=pathlib.Path(tmp)/'output'
   proc=subprocess.run([sys.executable,str(ROOT/'scripts/weekly_refresh.py'),'--site-dir',str(site),'--output-dir',str(out),'--top','150','--published-site',NO_SITE],capture_output=True,text=True)
   self.assertEqual(proc.returncode,2);result=json.loads(proc.stdout);self.assertIn('top',result['error']);self.assertTrue(result['current_site_preserved']);self.assertFalse(out.exists())


class TwoTierRefreshTests(unittest.TestCase):
 """--depth all (the default): the head (--verify-top rows) is read twice as before; the tail is read once in pass A."""
 TOTALS={'day':12195,'week':32694,'month':66772}
 def test_default_reads_the_whole_day_and_week_and_20000_month_rows_verifying_only_the_heads(self):
  p=Provider(0,totals=self.TOTALS);sleeps=[];data=w.refresh_dashboard(prior(2000),client_factory=p.factory,sleeper=sleeps.append)
  self.assertEqual(sleeps,[910])
  for f in data['feeds']:
   c=f['coverage'];full=f['period']!='month';total=self.TOTALS[f['period']] if full else 20000;pages=-(-total//100)
   self.assertEqual(len(f['rows']),total);self.assertEqual(c['unique_repos'],total);self.assertEqual(c['reached_end'],full);self.assertEqual(c['pages_fetched'],pages)
   self.assertEqual((c['depth'],c['verified_top'],c['verified_rows'],c['verified_passes'],c['status']),('all' if full else 20000,2000,2000,['A','B'],'complete' if full else 'partial'))
   self.assertEqual(c.get('bounded_top'),None if full else 20000);self.assertEqual(c['tail']['rows'],total-2000);self.assertEqual(c['tail']['pages_fetched'],pages-20);self.assertEqual(c['tail']['duplicates'],0);self.assertEqual(c['tail']['total_drift'],0)
   self.assertEqual([r['repo_id'] for r in f['rows']],list(range(1000,1000+total)))  # head then tail, rank order
   self.assertEqual(f['rows'][0]['stars'],10**6+2);self.assertEqual(f['rows'][2000]['stars'],10**6-2000+2)  # head from pass B, tail read after it
   self.assertEqual(f['rows'][2000]['observation']['page'],21);self.assertEqual(f['rows'][0]['created_at'],'2020-01-01T00:00:00Z');self.assertIsNone(f['rows'][2000]['created_at'])
   self.assertEqual([(r['pass'],r['pages'],r['retries']) for r in c['reads']],[('A',20,0),('B',20,0),('tail',pages-20,0)])
  self.assertEqual(len(p.pages),-(-self.TOTALS['day']//100)+-(-self.TOTALS['week']//100)+200+60)  # heads twice, then each tail once
  self.assertEqual(p.pages[:2],[('day',1),('day',2)]);self.assertEqual(p.pages.index(('week',1)),20);self.assertEqual(p.pages.index(('day',21)),120)  # all heads A, all heads B, then tails
 def test_tail_total_drift_is_tolerated_and_recorded(self):
  p=Provider(12195,drift=(80,12260));data=w.refresh_dashboard(prior(2000),client_factory=p.factory,sleeper=lambda s:None)
  c=data['feeds'][0]['coverage'];self.assertEqual((c['reported_total'],c['last_reported_total'],c['status']),(12195,12260,'inconsistent'))
  self.assertEqual(c['tail']['total_drift'],65);self.assertIn('tail: provider total changed from 12195 to 12260 at page 80',c['warnings']);self.assertEqual(len(data['feeds'][0]['rows']),12260)
 def test_tail_under_98_percent_of_the_reported_total_fails_closed(self):
  # Pages 21..122 each lose 3 rows (306 rows, 2.5%): refused. Losing 2 rows each (204 rows, 1.7%) is published with the shortfall recorded.
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: 11889 unique rows is under 98% of the 12195 reported; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=Provider(12195,short_pages=(21,3)).factory,sleeper=lambda s:None)
  data=w.refresh_dashboard(prior(2000),client_factory=Provider(12195,short_pages=(21,2)).factory,sleeper=lambda s:None)
  f=data['feeds'][0];self.assertEqual(len(f['rows']),11991);self.assertEqual(f['coverage']['tail']['required_rows'],11952);self.assertEqual(f['coverage']['status'],'inconsistent')
  self.assertIn('tail: page 21 returned 98, expected 100',f['coverage']['warnings']);self.assertEqual(len(f['coverage']['warnings']),102)
 def test_tail_duplicates_keep_the_best_rank_and_count_against_coverage(self):
  p=Provider(12195,duplicate_page=30)  # page 30's first row repeats id 1000 (rank 1): the head row stays, the tail loses one unique row
  data=w.refresh_dashboard(prior(2000),client_factory=p.factory,sleeper=lambda s:None)
  f=data['feeds'][0];ids=[r['repo_id'] for r in f['rows']];self.assertEqual(len(ids),len(set(ids)));self.assertEqual(len(ids),12194)
  self.assertEqual(f['rows'][0]['observation']['page'],1);self.assertEqual(f['coverage']['tail']['duplicates'],0);self.assertEqual(f['coverage']['tail']['overlap_with_verified_top'],1)
  p=Provider(12195,duplicate_page=30);p.duplicate_page=None
  def tail_duplicate(url):
   payload=p(url);q=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
   if int(q['page'])==40:payload['data'][0]=dict(payload['data'][0],id=1000+29*100)  # repeats page 30's first id
   return payload
  data=w.refresh_dashboard(prior(2000),client_factory=lambda **kw:FeedClient(fetch=tail_duplicate,**kw),sleeper=lambda s:None)
  f=data['feeds'][0];c=f['coverage'];self.assertEqual(c['tail']['duplicates'],1);self.assertEqual(c['unique_repos'],12194)
  self.assertEqual(next(r for r in f['rows'] if r['repo_id']==1000+29*100)['observation']['page'],30);self.assertIn('tail: 1 duplicate provider IDs across pages; best-ranked observed row retained',c['warnings'])
 def test_tail_fetch_failure_or_stop_before_the_end_fails_closed(self):
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: tail page 50 failed again after re-queue: URLError: <urlopen error synthetic connection reset>; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=lambda **kw:Provider(12195,fail_page=50).factory(max_attempts=1,**kw),sleeper=lambda s:None)
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: tail stopped after 80 pages before the provider end; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=Provider(12195).factory,max_pages=100,sleeper=lambda s:None)
 def test_head_failure_in_pass_a_fails_closed_even_when_b_and_c_agree(self):
  # Codex full-lists review #1: pass A fails on head page 5, so no tail is read; B and C then agree on the head. That must never
  # publish the 2,000-row head as the whole feed.
  p=Provider(12195);failed=[]
  def flaky(url):
   q=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
   if (q['time'],q['page'])==('day','5') and not failed:failed.append(url);raise urllib.error.URLError('synthetic connection reset')
   return p(url)
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: pass A head URLError: <urlopen error synthetic connection reset>; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=lambda **kw:FeedClient(fetch=flaky,max_attempts=1,**kw),sleeper=lambda s:None)
  self.assertEqual(len(failed),1)
 def test_max_pages_at_the_head_never_publishes_the_head_as_the_whole_feed(self):
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: no tail read: the 20-page bound stops at the head before the provider end; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=Provider(12195).factory,max_pages=20,sleeper=lambda s:None)
 def test_head_mismatch_retries_only_the_head_and_the_tail_is_read_after_it(self):
  p=Provider(12195,drop_after=('day',1));sleeps=[];data=w.refresh_dashboard(prior(2000),client_factory=p.factory,sleeper=sleeps.append)
  day=data['feeds'][0];ids=[r['repo_id'] for r in day['rows']]
  self.assertEqual(sleeps,[910,910]);self.assertEqual(day['coverage']['verified_passes'],['B','C']);self.assertEqual(ids[:2000],list(range(1001,3001)))
  self.assertEqual(ids,list(range(1001,1000+12196)));self.assertEqual(day['coverage']['tail']['overlap_with_verified_top'],0)  # old rank 2001 moved into the head; the tail starts at old rank 2002
  self.assertEqual(len(p.pages),122*3+60+20)
 def test_head_passes_that_keep_disagreeing_fail_closed_before_any_tail_is_used(self):
  p=Provider(12195,shift_every_pass=True)
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: membership or order changed between passes; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=p.factory,sleeper=lambda s:None)
 def test_shrink_guard_is_half_of_the_previous_published_rows(self):
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: 12195 rows is under half of min\\(depth, previous 30000\\); current site unchanged'):
   w.refresh_dashboard(prior(30000),client_factory=Provider(12195).factory,sleeper=lambda s:None)
  self.assertEqual(len(w.refresh_dashboard(prior(2000),client_factory=Provider(12195).factory,sleeper=lambda s:None)['feeds'][0]['rows']),12195)  # growth from 2,000 is fine
  self.assertEqual(len(w.refresh_dashboard(prior(24000),client_factory=Provider(12195).factory,sleeper=lambda s:None)['feeds'][0]['rows']),12195)
 def test_bounded_depth_keeps_todays_behaviour_and_alias(self):
  p=Provider(12195);data=w.refresh_dashboard(prior(300),client_factory=p.factory,depth=2000,verify_top=2000,sleeper=lambda s:None)
  f=data['feeds'][0];self.assertEqual((len(f['rows']),f['coverage']['bounded_top'],f['coverage']['verified_passes'],f['coverage']['tail'],f['coverage']['reached_end']),(2000,2000,['A','B'],None,False))
  self.assertEqual(len(p.pages),120)
  p=Provider(12195);data=w.refresh_dashboard(prior(300),client_factory=p.factory,depth=3000,verify_top=2000,sleeper=lambda s:None)
  f=data['feeds'][0];self.assertEqual((len(f['rows']),f['coverage']['bounded_top'],f['coverage']['verified_rows'],f['coverage']['tail']['pages_fetched'],f['coverage']['reached_end'],f['coverage']['status']),(3000,3000,2000,10,False,'partial'))
  self.assertEqual(len(p.pages),30*3+60)
 def test_checkpoints_keep_head_tail_verify_and_retry_apart(self):
  p=Provider(12195,drop_after=('day',1))
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(2000));out=pathlib.Path(tmp)/'output';cp=pathlib.Path(tmp)/'cp'
   with patch.object(w.time,'sleep',lambda s:None):w.stage_site(site,out,'refresh',client_factory=p.factory,checkpoint_dir=cp,created_dates=lambda data:{'requested':0},published=published_of(tmp))
   self.assertEqual(sorted(d.name for d in cp.iterdir()),['day','day-retry','day-tail','day-verify','month','month-tail','month-verify','week','week-tail','week-verify'])
   self.assertTrue((cp/'day-tail'/'day-100-000021.json').exists());self.assertTrue((cp/'day-tail'/'day-100-000122.json').exists());self.assertFalse((cp/'day-tail'/'day-100-000020.json').exists())
 def test_cli_depth_and_verify_top_are_validated(self):
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(1));out=pathlib.Path(tmp)/'output'
   for args in (['--depth','150'],['--depth','x'],['--verify-top','150'],['--top','150']):
    proc=subprocess.run([sys.executable,str(ROOT/'scripts/weekly_refresh.py'),'--site-dir',str(site),'--output-dir',str(out),'--published-site',NO_SITE,*args],capture_output=True,text=True)
    self.assertNotEqual(proc.returncode,0,args);self.assertFalse(out.exists())
 def test_staged_artifact_for_the_full_lists_stays_far_under_300_mb(self):
  # 12,195 + 32,694 + 66,772 rows (the 2026-10-28 provider totals) through the real staging, twice so the second artifact also carries
  # the first one's full window files as its previous generation: six window files, one snapshot of the verified heads only.
  # GitHub Pages' site limit is 1 GB; a full-row snapshot per day would have been ~390 MB.
  with tempfile.TemporaryDirectory() as tmp:
   site=checkout(tmp);pub=publish(tmp,prior(2000));first=pathlib.Path(tmp)/'first';out=pathlib.Path(tmp)/'output'
   with patch.object(w.time,'sleep',lambda s:None):
    w.stage_site(site,first,'refresh',client_factory=Provider(0,totals=self.TOTALS).factory,created_dates=lambda data:{'requested':0},published=live(pub),depth='all')
    result=w.stage_site(site,out,'refresh',client_factory=Provider(0,totals={k:v+1 for k,v in self.TOTALS.items()}).factory,created_dates=lambda data:{'requested':0},published=LocalSite(first),depth='all')  # month_depth=all: the worst case
   self.assertEqual([x['rows'] for x in result['periods']],[12196,32695,66773]);self.assertLess(result['artifact_bytes'],300*1024*1024)
   self.assertLess(sum(f.stat().st_size for f in (out/'api').rglob('*') if f.is_file()),40*1024*1024)
   self.assertEqual(len(list((out/'api/v1/snapshots').iterdir())),1);self.assertEqual(len([n for n in out.iterdir() if n.name.startswith('data-')]),6)
   print(f"\nartifact {result['artifact_bytes']/1e6:.1f} MB for {sum(x['rows'] for x in result['periods'])} rows; window files {result['data_files']}")


def flaky(provider,failures):
 """provider wrapped so each (period, page) in failures raises URLError for its first n reads."""
 seen={}
 def fetch(url):
  q=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query));key=(q['time'],int(q['page']))
  seen[key]=seen.get(key,0)+1
  if seen[key]<=failures.get(key,0):raise urllib.error.URLError('synthetic 503 burst')
  return provider(url)
 return lambda **kw:FeedClient(fetch=fetch,**kw)
class ResilientReadTests(unittest.TestCase):
 """Seeker's 2026-10-28 live run: heads first and tails last, re-queued tail pages, fail fast, per-window depth, a time budget."""
 def test_a_tail_page_failing_all_retries_is_requeued_once_and_recovers(self):
  p=Provider(12195);waits=[]
  data=w.refresh_dashboard(prior(2000),client_factory=flaky(p,{('day',50):6,('week',30):3}),sleeper=waits.append,depth='all')
  day,week=data['feeds'][0],data['feeds'][1]
  self.assertEqual(len(day['rows']),12195);self.assertEqual([r['repo_id'] for r in day['rows']],list(range(1000,13195)))  # rank order kept
  self.assertEqual(day['rows'][4900]['observation']['page'],50)
  tail=day['coverage']['reads'][-1];self.assertEqual((tail['pass'],tail['requeued_pages'],tail['retries']),('tail',[50],5))
  self.assertEqual(week['coverage']['reads'][-1]['retries'],3);self.assertEqual(week['coverage']['reads'][-1]['requeued_pages'],[])
  self.assertEqual(waits,[910]+[5,15,30,60,120]+[5,15,30])  # B wait, day page 50's five retries, week page 30's three
 def test_a_tail_page_failing_twice_fails_the_window_and_stops_all_further_reads(self):
  p=Provider(12195)
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: tail page 50 failed again after re-queue: URLError: .*synthetic 503 burst.*; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=flaky(p,{('day',50):12}),sleeper=lambda s:None,depth='all')
  self.assertEqual(max(n for t,n in p.pages if t=='day'),122);self.assertEqual(max(n for t,n in p.pages if t!='day'),20)  # no week/month tail read after day failed
  self.assertEqual(len(p.pages),60+60+101)  # heads A and B, day tail pages 21..122 except page 50 (its 12 reads all failed)
 def test_unstable_heads_fail_before_any_tail_is_read(self):
  p=Provider(12195,shift_every_pass=True)
  with self.assertRaisesRegex(ValueError,'day refresh not publishable: membership or order changed between passes; current site unchanged'):
   w.refresh_dashboard(prior(2000),client_factory=p.factory,sleeper=lambda s:None,depth='all')
  self.assertEqual(max(n for _,n in p.pages),20);self.assertEqual(len(p.pages),180)  # heads A, B, C only
 def test_heads_are_read_back_to_back_and_tails_last(self):
  p=Provider(12195);sleeps=[]
  w.refresh_dashboard(prior(2000),client_factory=p.factory,sleeper=lambda s:(sleeps.append((s,len(p.pages)))),depth='all')
  self.assertEqual(sleeps,[(910,60)]);self.assertEqual(p.pages[60:62],[('day',1),('day',2)]);self.assertEqual(p.pages[120],('day',21))
  self.assertEqual(sorted(set(t for t,n in p.pages[120:] )),['day','month','week']);self.assertTrue(all(n>20 for _,n in p.pages[120:]))
 def test_depth_per_window(self):
  self.assertEqual(w.parse_depth(None),{'day':'all','week':'all','month':20000})
  self.assertEqual(w.parse_depth('all'),{'day':'all','week':'all','month':'all'});self.assertEqual(w.parse_depth('3000'),{'day':3000,'week':3000,'month':3000})
  self.assertEqual(w.parse_depth('day=all,week=all,month=20000'),{'day':'all','week':'all','month':20000});self.assertEqual(w.parse_depth('month=all'),{'day':'all','week':'all','month':'all'})
  for bad in ('year=all','day=x','month=150','day=all,month=0'):
   with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'depth'):w.resolve_depth(None,bad,2000,1000)
  p=Provider(0,totals={'day':12195,'week':12195,'month':12195})
  data=w.refresh_dashboard(prior(300),client_factory=p.factory,sleeper=lambda s:None,depth='day=all,week=3000,month=2500',verify_top=2000)
  self.assertEqual([len(f['rows']) for f in data['feeds']],[12195,3000,2500]);self.assertEqual([f['coverage'].get('bounded_top') for f in data['feeds']],[None,3000,2500])
  self.assertEqual(w.refresh_dashboard(prior(300),client_factory=Provider(12195).factory,sleeper=lambda s:None,top=2000)['feeds'][2]['coverage']['bounded_top'],2000)  # --top N stays every window
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(1));out=pathlib.Path(tmp)/'output'
   proc=subprocess.run([sys.executable,str(ROOT/'scripts/weekly_refresh.py'),'--site-dir',str(site),'--output-dir',str(out),'--published-site',NO_SITE,'--depth','year=all'],capture_output=True,text=True)
   self.assertEqual(proc.returncode,2);self.assertIn("unknown window 'year'",json.loads(proc.stdout)['error']);self.assertFalse(out.exists())
 def test_time_budget_stops_before_the_page_or_wait_that_would_pass_it(self):
  clock=[0.0];p=Provider(12195)
  def fetch(url):clock[0]+=10;return p(url)
  factory=lambda **kw:FeedClient(fetch=fetch,**kw)
  with self.assertRaisesRegex(ValueError,'^time budget exhausted at week page 11; current site unchanged$'):
   w.refresh_dashboard(prior(2000),client_factory=factory,sleeper=lambda s:clock.__setitem__(0,clock[0]+s),deadline_minutes=5,clock=lambda:clock[0],depth='all')
  self.assertEqual(len(p.pages),30)  # day head 20 pages + week 10 pages at 10 s each: the 31st read is never made
  clock[0]=0.0;p.pages.clear()
  with self.assertRaisesRegex(ValueError,'time budget exhausted at the wait before pass B \\(a 910 s wait would pass the deadline\\)'):
   w.refresh_dashboard(prior(2000),client_factory=factory,sleeper=lambda s:clock.__setitem__(0,clock[0]+s),deadline_minutes=15,clock=lambda:clock[0],depth='all')
  self.assertEqual(len(p.pages),60)
  with tempfile.TemporaryDirectory() as tmp:
   site=site_fixture(tmp,prior(2000));out=pathlib.Path(tmp)/'output';clock[0]=0.0
   with self.assertRaisesRegex(ValueError,'time budget exhausted at the wait before pass B .*; current site unchanged'):
    w.stage_site(site,out,'refresh',client_factory=factory,created_dates=lambda d:self.fail('network'),published=published_of(tmp),deadline_minutes=5,depth='all')
   self.assertFalse(out.exists())
