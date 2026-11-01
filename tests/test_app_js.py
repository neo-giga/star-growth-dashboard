"""Exercise the app JS helpers under node: the page file parser, sorting (nulls last both ways), the sort-state machine, the filters and their URL state, Link-count parsing, GitHub outcome mapping, the repository record, labels."""
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
 def test_select_rows_sorts_nulls_last_in_both_directions_and_only_by_date_stars_or_a_gain(self):
  rows=json.dumps([{'full_name':'b/two','language':'Go','created_at':'2020-01-02T00:00:00Z','stars':20,'gain_7d':5,'history_mask':0},{'full_name':'a/one','language':None,'created_at':'2021-01-02T00:00:00Z','stars':10,'gain_7d':None,'history_mask':0},
   {'full_name':'c/three','language':'Rust','created_at':None,'stars':30,'gain_7d':9,'history_mask':0},{'full_name':'d/four','language':'Rust','created_at':'2019-01-02T00:00:00Z','stars':30,'gain_7d':9,'history_mask':0}])
  out=run(f"""const rows={rows};const names=o=>app.selectRows(rows,o).map(r=>r.full_name);console.log(JSON.stringify([names({{sort:'gain_7d',direction:'desc'}}),names({{sort:'gain_7d',direction:'asc'}}),names({{sort:'created_at',direction:'desc'}}),names({{sort:'created_at',direction:'asc'}}),
   names({{sort:'stars',direction:'desc',search:'T'}}),names({{language:'Rust'}}),names({{language:'Unknown'}}),
   app.nextSort({{sort:'gain_7d',direction:'desc'}},'gain_7d'),app.nextSort({{sort:'gain_7d',direction:'asc'}},'gain_7d'),app.nextSort({{sort:'gain_7d',direction:'asc'}},'stars'),app.nextSort({{sort:'stars',direction:'desc'}},'created_at'),app.DEFAULT_SORT,app.RETIRED_SORTS]))""")
  self.assertEqual(out,[['c/three','d/four','b/two','a/one'],['b/two','c/three','d/four','a/one'],['a/one','b/two','d/four','c/three'],['d/four','b/two','a/one','c/three'],
   ['c/three','b/two'],['c/three','d/four'],['a/one'],
   {'sort':'gain_7d','direction':'asc'},{'sort':'gain_7d','direction':'desc'},{'sort':'stars','direction':'desc'},{'sort':'created_at','direction':'desc'},'gain_7d',['full_name','language']])
 def test_a_pending_gain_sorts_last_in_both_directions_while_a_known_gain_of_a_pending_row_ranks_by_value(self):
  rows=json.dumps([{'full_name':'a/pending','stars':5,'gain_1d':900,'gain_7d':None,'pending':True},{'full_name':'b/known','stars':9,'gain_1d':1,'gain_7d':1,'pending':False},
   {'full_name':'c/unknown','stars':1,'gain_1d':None,'gain_7d':None,'pending':False},{'full_name':'d/pending-zero','stars':2,'gain_1d':0,'gain_7d':None,'pending':True}])
  out=run(f"""const rows={rows};const names=o=>app.selectRows(rows,o).map(r=>r.full_name);console.log(JSON.stringify([names({{sort:'gain_7d',direction:'desc'}}),names({{sort:'gain_7d',direction:'asc'}}),names({{sort:'gain_1d',direction:'desc'}}),names({{sort:'gain_1d',direction:'asc'}})]))""")
  self.assertEqual(out,[['b/known','a/pending','d/pending-zero','c/unknown'],['b/known','a/pending','d/pending-zero','c/unknown'],['a/pending','b/known','d/pending-zero','c/unknown'],['d/pending-zero','b/known','a/pending','c/unknown']])
 def test_count_loader_rejects_unsafe_names_without_fetching(self):
  self.assertEqual(run('''(async()=>{let n=0;const load=app.createCountLoader(async()=>{n++;},{getItem:()=>null,setItem(){}});try{await load('../evil');console.log('no')}catch(e){console.log(JSON.stringify([e.message,n,app.apiRepoPath('o/.'),app.apiRepoPath('a/b c'),app.apiRepoPath('morluto/rea')]))}})()'''),['Invalid repository name',0,None,None,'morluto/rea'])
 def test_a_column_nulled_by_a_missed_capture_gets_its_header_note(self):
  w=lambda **k:dict({'column':'gain_7d','days':7,'source':'history','exact_since':'2026-11-05','baseline_date':None},**k)
  self.assertEqual(run(f"""console.log(JSON.stringify([app.windowNote({json.dumps(w())},'2026-11-04'),app.windowNote({json.dumps(w())},'2026-11-05'),app.windowNote({json.dumps(w(column='gain_1d',days=1,exact_since='2026-10-30'))},'2026-11-01'),
   app.windowNote({json.dumps(w(source='snapshot',baseline_date='2026-10-30'))},'2026-11-06'),app.windowNote({json.dumps(w(exact_since=None))},'2026-11-06')]))"""),
   [None,'Not available today: the capture 7 days ago was missed','Not available today: the capture 1 day ago was missed',None,None])
 def test_pending_history_rows_the_capture_status_line_and_the_pending_line(self):
  fixture=json.loads((ROOT/'tests/fixtures/windows-sample.json').read_text());columns={k:v for k,v in fixture['columns'].items() if k!='node_id'}
  columns['history_pending']=[1,2]+[0]*198
  page=dict(fixture,schema='star-growth-dashboard/page/1',threshold=999,min_stars=1000,columns=columns,capture={'status':'partial','reasons':['discovery partial'],'rows':66900,'search_total':66915})
  out=run(f"""const d=app.parseData({json.dumps(page)});const old=app.parseData({json.dumps(dict(page,columns={k:v for k,v in columns.items() if k!='history_pending'},capture=None,min_stars=None))});
   console.log(JSON.stringify([d.pending,d.retrying,d.rows[0].pending,d.rows[2].pending,d.min_stars,app.captureNote(d),old.pending,old.min_stars,app.captureNote(old),
   app.captureNote(app.parseData({json.dumps(dict(page,capture={'status':'complete','reasons':[],'rows':66914,'search_total':66915}))})),app.pendingNote(d),app.pendingNote(old),app.pendingNote({{...d,retrying:0}}),app.pendingNote(null)]))""")
  self.assertEqual(out,[2,1,True,False,1000,'Capture partial: discovery partial · 66,900 rows vs GitHub search total 66,915.',0,1000,None,'Capture complete: every discovery slice listed and every count read · 66,914 rows vs GitHub search total 66,915.',
   "Gains for 2 recently added repositories (1 retried after a failed read) aren't known yet; they appear once their star history is read.",None,
   "Gains for 2 recently added repositories aren't known yet; they appear once their star history is read.",None])
 def test_a_pending_gain_is_a_muted_dash_whose_title_names_no_date_and_points_assistive_tech_at_the_pending_line(self):
  out=run('console.log(JSON.stringify([app.pendingTitle(),app.pendingTitle(true),app.displayNumber(null,true),app.UNKNOWN_LANGUAGE]))')
  self.assertEqual(out,["Not known yet: this repository joined recently and its GitHub star history is read gradually. Its stars are current; this gain appears once its history is read.",
   "Not known yet: a read of this repository's GitHub star history failed or came back incomplete; it is retried in turn. Its stars are current; this gain appears once its history is read.",'—','Unknown'])
  self.assertNotIn('pendingSince',(ROOT/'app.js').read_text());self.assertIn("cell.setAttribute('aria-describedby', 'pending-note')",(ROOT/'app.js').read_text())
 def test_the_page_is_named_momentum_lists_nine_sorts_and_never_says_pending(self):
  html=(ROOT/'index.html').read_text();visible=re.sub(r'<script>.*?</script>','',html,flags=re.S)
  self.assertEqual(re.search(r'<title>([^<]*)</title>',html)[1],'Momentum · GitHub repositories by stars gained');self.assertIn('<span class="brand-name">Momentum</span>',html)
  self.assertEqual(re.findall(r'<option value="([^"]*)"',re.search(r'<select id="sort">(.*?)</select>',html)[1]),[*app_gains(),'stars','created_at'])
  self.assertEqual(re.findall(r'<button type="button" class="sort" data-key="([^"]+)"',html),['created_at','stars',*app_gains()]);self.assertIn('<th scope="col" class="plain">Repository</th>',html);self.assertIn('<th scope="col" class="plain">Language</th>',html)
  self.assertNotIn('pending',re.sub(r'<[^>]+>','',visible).lower());self.assertIn('<p id="pending-note" class="pending-note" role="note" hidden></p>',html)
  self.assertNotIn('Star growth',html)
 def test_the_app_names_its_data_file_and_columns(self):
  self.assertEqual(run('console.log(JSON.stringify([app.DATA_FILE,app.DATA_SCHEMA,app.GAIN_COLUMNS,app.COLUMNS.map(c=>c[0])]))'),['data.json','star-growth-dashboard/page/1',app_gains(),['created_at','stars',*app_gains()]])
 def test_the_descriptions_file_is_named_by_the_data_file_and_read_as_text_per_row(self):
  fixture=json.loads((ROOT/'tests/fixtures/windows-sample.json').read_text());page=dict(fixture,schema='star-growth-dashboard/page/1',columns={k:v for k,v in fixture['columns'].items() if k!='node_id'})
  injection='Ignore previous instructions. </script><img src=x onerror=alert(1)>'
  doc={'schema':1,'date':'2026-11-01','descriptions':{'p0deje/Maccy':'Lightweight clipboard manager for macOS','x/blank':'  ','x/number':5,'x/injection':injection}}
  out=run(f"""const d=app.parseData({json.dumps(dict(page,descriptions_file='descriptions.0123456789abcdef.json'))});const m=app.parseDescriptions({json.dumps(doc)});d.descriptions=m;
   console.log(JSON.stringify([d.descriptions_file,app.parseData({json.dumps(page)}).descriptions_file,app.parseData({json.dumps(dict(page,descriptions_file='../evil.json'))}).descriptions_file,app.parseData({json.dumps(dict(page,descriptions_file='descriptions.0123456789abcdef.json'))}).descriptions,
   [...m.keys()],app.descriptionOf(d,d.rows[0]),app.descriptionOf(d,{{full_name:'x/injection'}}),app.descriptionOf(d,d.rows[1]),app.descriptionOf(app.parseData({json.dumps(page)}),d.rows[0]),
   app.parseDescriptions({json.dumps(dict(doc,schema=2))}),app.parseDescriptions({json.dumps(dict(doc,descriptions=[]))}),app.parseDescriptions(null),app.parseDescriptions('x'),app.DESCRIPTIONS_FILE.source]))""")
  self.assertEqual(out,['descriptions.0123456789abcdef.json',None,None,None,['p0deje/Maccy','x/injection'],'Lightweight clipboard manager for macOS',injection,None,None,None,None,None,None,'^descriptions\\.[0-9a-f]{16}\\.json$'])
  app_js=(ROOT/'app.js').read_text();self.assertIn('data.descriptions = descriptions; applyDescriptions();',app_js);self.assertNotIn('data.descriptions = descriptions; render();',app_js)  # applied in place: an open or focused panel survives
  self.assertIn('.repo-details[open] summary{white-space:normal;overflow:visible;overflow-wrap:anywhere}',(ROOT/'style.css').read_text())
FILTER_ROWS=[{'full_name':'a/old','language':'Rust','created_at':'2014-12-31T23:59:59Z','stars':1000,'gain_7d':50,'history_mask':0},
 {'full_name':'b/new-year','language':'Rust','created_at':'2024-01-01T00:00:00Z','stars':1500,'gain_7d':5,'history_mask':0},
 {'full_name':'c/offset','language':'Go','created_at':'2024-12-31T23:30:00-02:00','stars':1501,'gain_7d':900,'history_mask':0},
 {'full_name':'d/none','language':None,'created_at':None,'stars':None,'gain_7d':None,'history_mask':0},
 {'full_name':'e/empty','language':'','created_at':'2024-06-01T00:00:00Z','stars':20000,'gain_7d':1,'history_mask':0}]
@unittest.skipUnless(shutil.which('node'),'node not installed')
class FilterTests(unittest.TestCase):
 def names(self,options):
  return run(f'console.log(JSON.stringify(app.filterRows({json.dumps(FILTER_ROWS)},{json.dumps(options)}).map(r=>r.full_name)))')
 def test_created_bounds_are_inclusive_utc_dates_and_unknown_never_matches(self):
  self.assertEqual(self.names({'created':{'after':'2024-01-01','before':None}}),['b/new-year','c/offset','e/empty'])
  self.assertEqual(self.names({'created':{'after':None,'before':'2014-12-31'}}),['a/old'])
  self.assertEqual(self.names({'created':{'after':'2024-01-01','before':'2024-12-31'}}),['b/new-year','e/empty'])  # c/offset is 2025-01-01 in UTC
  self.assertEqual(self.names({'created':{'after':'2025-01-01','before':'2025-01-01'}}),['c/offset'])
  self.assertEqual(self.names({'created':{'after':'2025-02-01','before':'2025-01-01'}}),[])
 def test_stars_bounds_are_inclusive_and_unknown_never_matches(self):
  self.assertEqual(self.names({'stars':{'min':1000,'max':1500}}),['a/old','b/new-year'])
  self.assertEqual(self.names({'stars':{'min':1501,'max':None}}),['c/offset','e/empty'])
  self.assertEqual(self.names({'stars':{'min':None,'max':999}}),[])
 def test_language_unknown_matches_only_null_or_empty_and_filters_and_combine(self):
  self.assertEqual(self.names({'language':'Unknown'}),['d/none','e/empty'])
  self.assertEqual(self.names({'language':'Rust'}),['a/old','b/new-year'])
  self.assertEqual(self.names({'language':'rust'}),[])
  self.assertEqual(self.names({'language':'Rust','created':{'after':'2015-01-01','before':None},'stars':{'min':1500,'max':1500},'search':'NEW'}),['b/new-year'])
  self.assertEqual(self.names({}),[r['full_name'] for r in FILTER_ROWS])
 def test_filtering_precedes_sorting_and_paging(self):
  rows=[{'full_name':f'o/r{i:03d}','language':'Go' if i%2 else 'Rust','created_at':'2020-01-01T00:00:00Z','stars':1000+i,'gain_7d':i,'history_mask':0} for i in range(120)]
  out=run(f"""const rows={json.dumps(rows)};const s=app.selectRows(rows,{{sort:'gain_7d',direction:'desc',language:'Rust',stars:{{min:1010,max:null}}}});const p=app.pageOf(s,2,50),last=app.pageOf(s,9,50);
   console.log(JSON.stringify([s.length,s[0].full_name,p.page,p.pages,p.offset,p.rows.length,p.rows[0].full_name,last.page,last.rows.length,last.rows.at(-1).full_name,app.pageOf([],3).page]))""")
  # 55 Rust rows (even i) with stars >= 1010 (i >= 10): i = 10..118; the top of the filtered list is i=118, page 2 starts at the 51st (i=18)
  self.assertEqual(out,[55,'o/r118',2,2,50,5,'o/r018',2,5,'o/r010',1])
 def test_qualifier_ranges_parse_like_github_and_reject_the_rest(self):
  out=run('''console.log(JSON.stringify([">=2024-01-01","<=2024-12-31","2024-01-01..2024-12-31","2024-02-29",">2024-12-31","<2024-03-01","*..2024-01-01","2024-01-01..*",
   "2023-02-29","2024-1-01","*..*","yesterday","",">=","2024-01-01..2024-12-31..2025-01-01"].map(app.parseCreated)))''')
  self.assertEqual(out,[{'after':'2024-01-01','before':None},{'after':None,'before':'2024-12-31'},{'after':'2024-01-01','before':'2024-12-31'},{'after':'2024-02-29','before':'2024-02-29'},
   {'after':'2025-01-01','before':None},{'after':None,'before':'2024-02-29'},{'after':None,'before':'2024-01-01'},{'after':'2024-01-01','before':None},None,None,None,None,None,None,None])
  self.assertEqual(run('console.log(JSON.stringify([">=10000","1000..5000","<=999",">999","<1000","1500","-5","1.5","1e3","10,000","99999999999999999999"].map(app.parseStars)))'),
   [{'min':10000,'max':None},{'min':1000,'max':5000},{'min':None,'max':999},{'min':1000,'max':None},{'min':None,'max':999},{'min':1500,'max':1500},None,None,None,None,None])
 def test_url_state_round_trips_and_invalid_values_are_ignored_with_a_note(self):
  canonical=['','?q=react&language=Rust&created=2024-01-01..2024-12-31&stars=1000..1500&sort=stars&dir=asc&page=3','?created=%3E=2024-01-01','?created=%3C=2014-12-31&stars=%3E=10000','?language=Unknown','?q=a%20b%26c']
  out=run(f'console.log(JSON.stringify({json.dumps(canonical)}.map(q=>{{const p=app.parseQuery(q);return [app.serializeQuery(p.state),p.invalid.length]}})))')
  self.assertEqual(out,[[q,0] for q in canonical])
  out=run('''const p=app.parseQuery("?created=2024-13-01&stars=lots&sort=nope&dir=up&page=0&language=Go&utm=x");console.log(JSON.stringify([p.state,p.invalid.map(i=>i.slice(0,2)),app.serializeQuery(p.state),app.invalidNote(p.invalid).slice(0,64),app.invalidNote([]),
   app.activeFilters(app.parseQuery("?q=%20x%20&stars=%3E%3D5&created=2024-01-01").state),app.parseQuery("?created=>2024-01-01").state.created]))''')
  self.assertEqual(out,[{'search':'','language':'Go','created':{'after':None,'before':None},'stars':{'min':None,'max':None},'sort':'gain_7d','direction':'desc','page':1},
   [['created','2024-13-01'],['stars','lots'],['sort','nope'],['dir','up'],['page','0']],'?language=Go','Ignored from this link: created=2024-13-01 (expected a UTC date ',None,
   [['q','x'],['created','2024-01-01..2024-01-01'],['stars','>=5']],{'after':'2024-01-02','before':None}])
  # the sorts the page no longer offers fall back to the default sort silently; the other parameters still apply
  self.assertEqual(run('console.log(JSON.stringify(["?sort=full_name&dir=asc&language=Go","?sort=language&page=2"].map(q=>{const p=app.parseQuery(q);return [p.state.sort,p.state.direction,p.state.language,p.state.page,p.invalid,app.serializeQuery(p.state)]})))'),
   [['gain_7d','asc','Go',1,[],'?language=Go&dir=asc'],['gain_7d','desc','',2,[],'?page=2']])
 def test_edits_made_while_the_data_loads_override_the_link_except_its_language(self):
  out=run('''const fromURL=app.parseQuery("?q=old&language=Rust&created=2024-01-01..2024-12-31&stars=%3E=5000&sort=stars&dir=asc&page=4").state;
   const edits={...fromURL,search:'react',language:'',created:{after:null,before:'2019-12-31'},stars:{min:2000,max:null},sort:'gain_30d',direction:'desc'};
   const kept=app.keepEarlyEdits(fromURL,edits),untouched=app.keepEarlyEdits(fromURL,{...fromURL,language:'',created:{...fromURL.created},stars:{...fromURL.stars}});
   console.log(JSON.stringify([kept,app.serializeQuery(kept),untouched===fromURL,untouched.page,app.keepEarlyEdits(fromURL,{...fromURL,direction:'desc'}).page]))''')
  self.assertEqual(out,[{'search':'react','language':'Rust','created':{'after':None,'before':'2019-12-31'},'stars':{'min':2000,'max':None},'sort':'gain_30d','direction':'desc','page':1},
   '?q=react&language=Rust&created=%3C=2019-12-31&stars=%3E=2000&sort=gain_30d',True,4,1])  # focus without a change keeps the link's page
