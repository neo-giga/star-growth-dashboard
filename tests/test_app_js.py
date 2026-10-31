"""Exercise the app JS helpers under node: the page file parser, sorting (nulls last both ways), the sort-state machine, Link-count parsing, GitHub outcome mapping, the repository record, labels."""
import json,pathlib,re,shutil,subprocess,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
APP=ROOT/re.search(r'<script[^>]+src="([^"]+)"',(ROOT/'index.html').read_text())[1]
def app_gains():
 return ['gain_1d','gain_7d','gain_15d','gain_30d','gain_90d','gain_180d','gain_365d']
def run(script):
 proc=subprocess.run(['node','-e',f"const app=require({json.dumps(str(APP))});{script}"],capture_output=True,text=True,check=True)
 return json.loads(proc.stdout)
@unittest.skipUnless(shutil.which('node'),'node not installed')
class AppJsTests(unittest.TestCase):
 def test_link_count_uses_last_page_else_body_length(self):
  link='<https://api.github.com/repositories/1/contributors?per_page=1&anon=true&page=2>; rel="next", <https://api.github.com/repositories/1/contributors?per_page=1&anon=true&page=344>; rel="last"'
  self.assertEqual(run(f'console.log(JSON.stringify([app.linkCount({json.dumps(link)},1),app.linkCount(null,1),app.linkCount(null,0),app.linkCount(undefined,-3),app.linkCount("<x?page=5>; rel=\\"next\\"",1)]))'),[344,1,0,0,1])
 def test_count_outcome_maps_statuses(self):
  script='''const h=o=>k=>o[k]??null;const r=[
   app.countOutcome(200,h({link:'<u?per_page=1&page=18210>; rel="last"'}),[{}]),
   app.countOutcome(200,h({}),[]),app.countOutcome(204,h({}),null),
   app.countOutcome(403,h({'x-ratelimit-remaining':'0','x-ratelimit-reset':'1793171427'}),{message:'API rate limit exceeded for 1.2.3.4.'}),
   app.countOutcome(429,h({}),{message:'You have exceeded a secondary rate limit'}),
   app.countOutcome(403,h({'x-ratelimit-remaining':'41'}),{message:'The history or contributor list is too large to list contributors for this repository via the API.'}),
   app.countOutcome(404,h({}),{message:'Not Found'}),app.countOutcome(451,h({}),null),app.countOutcome(500,h({}),null)];console.log(JSON.stringify(r))'''
  self.assertEqual(run(script),[
   {'value':18210,'cache':True,'limit':None},{'value':0,'cache':True,'limit':None},{'value':0,'cache':True,'limit':None},
   {'value':None,'cache':False,'limit':{'reset':1793171427000}},{'value':None,'cache':False,'limit':{'reset':None}},
   {'value':'too_many','cache':True,'limit':None},{'value':'unavailable','cache':True,'limit':None},{'value':'unavailable','cache':True,'limit':None},{'value':'unavailable','cache':False,'limit':None}])
 def test_labels(self):
  self.assertEqual(run('console.log(JSON.stringify([app.countLabel(18210),app.countLabel("too_many"),app.countLabel("unavailable"),app.countLabel(null),app.creationLabel("2026-04-14T00:51:32+00:00"),app.creationLabel(null),app.clockLabel(1793171427000),app.displayNumber(null),app.displayNumber(5,true)]))'),
   ['18,210','too many to count','unavailable','—','Apr 14, 2026','—','07:10 UTC','—','+5'])
 def test_count_loader_caches_definite_results_in_storage(self):
  script='''(async()=>{const store=new Map(),storage={getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v)};let calls=[];
   const headers=o=>({get:k=>o[k.toLowerCase()]??null});
   const fetcher=async url=>{calls.push(url);if(url.endsWith('/repos/morluto/rea'))return {status:200,headers:headers({}),json:async()=>({description:'A tool',topics:['a','b'],forks_count:12,created_at:'2026-04-14T00:51:32Z',pushed_at:'2026-10-27T10:00:00Z',archived:false,private:false})};
    if(url.includes('/contributors'))return {status:200,headers:headers({link:'<u?page=344>; rel="last"'}),json:async()=>[{}]};
    if(url.includes('/issues/'))return {status:403,headers:headers({'x-ratelimit-remaining':'0','x-ratelimit-reset':'1793171427'}),json:async()=>({message:'API rate limit exceeded'})};
    return {status:404,headers:headers({}),json:async()=>({message:'Not Found'})};};
   const load=app.createCountLoader(fetcher,storage,()=>1793170000000);const seen=[];
   const first=await load('morluto/rea',(f,v)=>seen.push([f,v]));const second=await load('morluto/rea');
   console.log(JSON.stringify({first,second,calls,stored:JSON.parse(store.get('gh-counts:morluto/rea')),seen}));})()'''
  out=run(script);repo={'description':'A tool','topics':['a','b'],'forks':12,'created_at':'2026-04-14T00:51:32Z','pushed_at':'2026-10-27T10:00:00Z','archived':False}
  self.assertEqual(out['first']['fields'],{'repo':repo,'contributors':344,'issue_comments':None,'pr_review_comments':'unavailable'});self.assertEqual(out['first']['limit'],{'reset':1793171427000});self.assertTrue(out['first']['fetched'])
  self.assertEqual(out['stored'],{'fetched_at':'2026-10-28T06:46:40.000Z','fields':{'repo':repo,'contributors':344,'pr_review_comments':'unavailable'}})
  self.assertEqual(out['calls'][:4],['https://api.github.com/repos/morluto/rea','https://api.github.com/repos/morluto/rea/contributors?per_page=1&anon=true','https://api.github.com/repos/morluto/rea/issues/comments?per_page=1','https://api.github.com/repos/morluto/rea/pulls/comments?per_page=1'])
  self.assertEqual(len(out['calls']),5);self.assertEqual(out['second']['fields']['contributors'],344);self.assertEqual(out['second']['fields']['repo'],repo);self.assertEqual(out['second']['limit'],{'reset':1793171427000})
 def test_repo_outcome_reduces_the_record_and_maps_failures_like_counts(self):
  script='''const h=o=>k=>o[k]??null;console.log(JSON.stringify([
   app.repoOutcome(200,h({}),{description:null,topics:[],forks_count:'7',created_at:'2020-01-01T00:00:00Z',pushed_at:null,archived:true,stargazers_count:5}),
   app.repoOutcome(200,h({}),[]),app.repoOutcome(404,h({}),{message:'Not Found'}),
   app.repoOutcome(403,h({'x-ratelimit-remaining':'0','x-ratelimit-reset':'1793171427'}),{message:'API rate limit exceeded'}),app.repoOutcome(500,h({}),null),
   app.metadataFields({description:' ',topics:[],forks:3,created_at:'2020-01-01T00:00:00Z',pushed_at:'bad',archived:false}),app.metadataFields('unavailable')]))'''
  self.assertEqual(run(script),[
   {'value':{'description':None,'topics':[],'forks':7,'created_at':'2020-01-01T00:00:00Z','pushed_at':None,'archived':True},'cache':True,'limit':None},
   {'value':'unavailable','cache':False,'limit':None},{'value':'unavailable','cache':True,'limit':None},
   {'value':None,'cache':False,'limit':{'reset':1793171427000}},{'value':'unavailable','cache':False,'limit':None},
   [['Description','None'],['Topics','None'],['Forks','3'],['Created (UTC)','2020-01-01 00:00:00 UTC'],['Last pushed (UTC)','—'],['Archived','No']],
   [['Description','—'],['Topics','—'],['Forks','—'],['Created (UTC)','—'],['Last pushed (UTC)','—'],['Archived','—']]])
 def test_parse_data_builds_rows_from_the_columnar_page_file(self):
  fixture=json.loads((ROOT/'tests/fixtures/windows-sample.json').read_text());page=dict(fixture,schema='star-growth-dashboard/page/1',columns={k:v for k,v in fixture['columns'].items() if k!='node_id'})
  out=run(f"""const d=app.parseData({json.dumps(page)});console.log(JSON.stringify([d.row_count,d.rows.length,d.rows[0].full_name,d.rows[0].stars,d.rows[0].gain_7d,d.rows[0].history_mask,typeof app.sortValue(d.rows[0],'created_at'),d.rows[0].search,d.windows.map(w=>w.column),d.threshold,app.windowTooltip(d.windows[1]),
   (()=>{{try{{app.parseData({{schema:'x'}})}}catch(e){{return e.message}}}})(),(()=>{{try{{app.parseData({json.dumps(dict(page,row_count=3))})}}catch(e){{return e.message}}}})()]))""")
  self.assertEqual(out,[200,200,'p0deje/Maccy',22138,109,127,'number','p0deje/maccy',app_gains(),2000,"From GitHub's star history; exact daily snapshots from 2026-11-05",'The local data file has an invalid format.','The local data file has an invalid format.'])
 def test_select_rows_sorts_nulls_last_in_both_directions_and_text_columns_by_name(self):
  rows=json.dumps([{'full_name':'b/two','language':'Go','created_at':'2020-01-02T00:00:00Z','stars':20,'gain_7d':5,'history_mask':0},{'full_name':'a/one','language':None,'created_at':'2021-01-02T00:00:00Z','stars':10,'gain_7d':None,'history_mask':0},
   {'full_name':'c/three','language':'Rust','created_at':None,'stars':30,'gain_7d':9,'history_mask':0},{'full_name':'d/four','language':'Rust','created_at':'2019-01-02T00:00:00Z','stars':30,'gain_7d':9,'history_mask':0}])
  out=run(f"""const rows={rows};const names=o=>app.selectRows(rows,o).map(r=>r.full_name);console.log(JSON.stringify([names({{sort:'gain_7d',direction:'desc'}}),names({{sort:'gain_7d',direction:'asc'}}),names({{sort:'created_at',direction:'desc'}}),names({{sort:'created_at',direction:'asc'}}),
   names({{sort:'language',direction:'asc'}}),names({{sort:'language',direction:'desc'}}),names({{sort:'full_name',direction:'asc'}}),names({{sort:'stars',direction:'desc',search:'T'}}),names({{language:'Rust'}}),names({{language:'—'}}),
   app.nextSort({{sort:'gain_7d',direction:'desc'}},'gain_7d'),app.nextSort({{sort:'gain_7d',direction:'asc'}},'gain_7d'),app.nextSort({{sort:'gain_7d',direction:'asc'}},'stars'),app.nextSort({{sort:'stars',direction:'desc'}},'full_name'),app.DEFAULT_SORT]))""")
  self.assertEqual(out,[['c/three','d/four','b/two','a/one'],['b/two','c/three','d/four','a/one'],['a/one','b/two','d/four','c/three'],['d/four','b/two','a/one','c/three'],
   ['b/two','c/three','d/four','a/one'],['c/three','d/four','b/two','a/one'],['a/one','b/two','c/three','d/four'],['c/three','b/two'],['c/three','d/four'],['a/one'],
   {'sort':'gain_7d','direction':'asc'},{'sort':'gain_7d','direction':'desc'},{'sort':'stars','direction':'desc'},{'sort':'full_name','direction':'asc'},'gain_7d'])
 def test_count_loader_rejects_unsafe_names_without_fetching(self):
  self.assertEqual(run('''(async()=>{let n=0;const load=app.createCountLoader(async()=>{n++;},{getItem:()=>null,setItem(){}});try{await load('../evil');console.log('no')}catch(e){console.log(JSON.stringify([e.message,n,app.apiRepoPath('o/.'),app.apiRepoPath('a/b c'),app.apiRepoPath('morluto/rea')]))}})()'''),['Invalid repository name',0,None,None,'morluto/rea'])
 def test_a_column_nulled_by_a_missed_capture_gets_its_header_note(self):
  w=lambda **k:dict({'column':'gain_7d','days':7,'source':'history','exact_since':'2026-11-05','baseline_date':None},**k)
  self.assertEqual(run(f"""console.log(JSON.stringify([app.windowNote({json.dumps(w())},'2026-11-04'),app.windowNote({json.dumps(w())},'2026-11-05'),app.windowNote({json.dumps(w(column='gain_1d',days=1,exact_since='2026-10-30'))},'2026-11-01'),
   app.windowNote({json.dumps(w(source='snapshot',baseline_date='2026-10-30'))},'2026-11-06'),app.windowNote({json.dumps(w(exact_since=None))},'2026-11-06')]))"""),
   [None,'Not available today: the capture 7 days ago was missed','Not available today: the capture 1 day ago was missed',None,None])
 def test_pending_history_rows_and_the_capture_status_line(self):
  fixture=json.loads((ROOT/'tests/fixtures/windows-sample.json').read_text());columns={k:v for k,v in fixture['columns'].items() if k!='node_id'}
  columns['history_pending']=[1,1]+[0]*198
  page=dict(fixture,schema='star-growth-dashboard/page/1',threshold=999,min_stars=1000,columns=columns,capture={'status':'partial','reasons':['discovery partial'],'rows':66900,'search_total':66915})
  out=run(f"""const d=app.parseData({json.dumps(page)});const old=app.parseData({json.dumps(dict(page,columns={k:v for k,v in columns.items() if k!='history_pending'},capture=None,min_stars=None))});
   console.log(JSON.stringify([d.pending,d.rows[0].pending,d.rows[2].pending,d.min_stars,app.captureNote(d),old.pending,old.min_stars,app.captureNote(old),
   app.captureNote(app.parseData({json.dumps(dict(page,capture={'status':'complete','reasons':[],'rows':66914,'search_total':66915}))})),app.PENDING]))""")
  self.assertEqual(out,[2,True,False,1000,'Capture partial: discovery partial · 66,900 rows vs GitHub search total 66,915 · history pending for 2 recently added repositories (read a few hundred a day; their unknown gains show "pending").',
   0,1000,None,'Capture complete: every discovery slice listed and every count read · 66,914 rows vs GitHub search total 66,915 · history pending for 2 recently added repositories (read a few hundred a day; their unknown gains show "pending").','pending'])
 def test_the_app_names_its_data_file_and_columns(self):
  self.assertEqual(run('console.log(JSON.stringify([app.DATA_FILE,app.DATA_SCHEMA,app.GAIN_COLUMNS,app.COLUMNS.map(c=>c[0])]))'),['data.json','star-growth-dashboard/page/1',app_gains(),['full_name','language','created_at','stars',*app_gains()]])
