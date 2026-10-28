"""Exercise the app JS helpers under node: Link-count parsing, GitHub outcome mapping, the repository record, labels."""
import json,pathlib,re,shutil,subprocess,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
APP=ROOT/re.search(r'<script[^>]+src="([^"]+)"',(ROOT/'index.html').read_text())[1]
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
 def test_parse_feed_requires_exactly_one_feed_of_the_window(self):
  self.assertEqual(run('''console.log(JSON.stringify([app.parseFeed({feeds:[{provider:'gittrend',period:'week',rows:[1]}]},'week').rows,app.DATA_FILES,(()=>{try{app.parseFeed({feeds:[{provider:'gittrend',period:'day',rows:[]}]},'week')}catch(e){return e.message}})(),(()=>{try{app.parseFeed({},'week')}catch(e){return e.message}})()]))'''),
   [[1],{'day':'data-day.json','week':'data-week.json','month':'data-month.json'},'The local data file does not hold exactly one week feed.','The local data file has an invalid format.'])
 def test_count_loader_rejects_unsafe_names_without_fetching(self):
  self.assertEqual(run('''(async()=>{let n=0;const load=app.createCountLoader(async()=>{n++;},{getItem:()=>null,setItem(){}});try{await load('../evil');console.log('no')}catch(e){console.log(JSON.stringify([e.message,n,app.apiRepoPath('o/.'),app.apiRepoPath('a/b c'),app.apiRepoPath('morluto/rea')]))}})()'''),['Invalid repository name',0,None,None,'morluto/rea'])
 def test_reload_once_per_missing_data_file(self):
  script='''const m=new Map(),s={getItem:k=>m.get(k)??null,setItem:(k,v)=>m.set(k,v)},broken={getItem(){throw new Error('denied')},setItem(){}},full={getItem:()=>null,setItem(){throw new Error('quota')}};
   console.log(JSON.stringify([app.reloadOnceFor(s,'data-month.a.json'),app.reloadOnceFor(s,'data-month.a.json'),app.reloadOnceFor(s,'data-day.b.json'),app.reloadOnceFor(null,'x'),app.reloadOnceFor(broken,'x'),app.reloadOnceFor(full,'x')]))'''
  self.assertEqual(run(script),[True,False,True,False,False,False])
