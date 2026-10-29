"""A tab left open across deploys (Codex full-lists review #5), in a real browser against a local static server, offline.

Runs only where playwright-core and its Chromium are installed (PLAYWRIGHT_CORE=<path to playwright-core>, or resolvable by node);
skipped otherwise, e.g. in the workflow's unit-check step.
"""
import http.server,json,os,pathlib,shutil,subprocess,tempfile,threading,unittest
from test_weekly_refresh import ROOT,LocalSite,Provider,checkout,prior,publish,w
from unittest.mock import patch
def playwright_core():
 path=os.environ.get('PLAYWRIGHT_CORE')
 if path:return path if pathlib.Path(path).is_dir() else None
 if not shutil.which('node'):return None
 proc=subprocess.run(['node','-e',"process.stdout.write(require.resolve('playwright-core'))"],capture_output=True,text=True)
 return str(pathlib.Path(proc.stdout).parent) if proc.returncode==0 else None
DRIVER=r'''
const {chromium}=require(process.argv[1]);const base=process.argv[2];
(async()=>{
 const browser=await chromium.launch();const page=await browser.newPage();const log={loads:0,requests:[],errors:[]};
 page.on('load',()=>log.loads++);page.on('request',r=>log.requests.push(new URL(r.url()).pathname));page.on('pageerror',e=>log.errors.push(String(e)));
 const count=async()=>{await page.waitForFunction(()=>/repositories$/.test(document.getElementById('result-count').textContent)||!document.getElementById('error').hidden,null,{timeout:10000});
  return {count:await page.textContent('#result-count'),error:await page.$eval('#error',e=>e.hidden?null:e.textContent),period:await page.$eval('#period',e=>e.value)};};
 await page.goto(base+'index.html');const opened=await count();
 const deployThenSelect=async(name,period,settle)=>{await fetch(base+'__deploy/'+name);log.requests.length=0;const before=log.loads;await page.selectOption('#period',period);
  await page.waitForTimeout(settle);await page.waitForLoadState('load');if(await page.$eval('#period',e=>e.value)!==period)await page.selectOption('#period',period);await page.waitForTimeout(300);
  return {state:await count(),reloads:log.loads-before,requests:log.requests.filter(p=>p.includes('data-'+period)).length};};
 const result={opened};
 result.one_deploy_later=await deployThenSelect('gen2','month',400);       // still on gen1's page: its month file is gen2's previous generation
 result.two_deploys_later=await deployThenSelect('gen3','day',1500);       // gen1's day file is gone from gen3
 await page.selectOption('#period','week');
 result.broken_deploy=await deployThenSelect('broken','month',1500);       // gen3's own month file missing from the deploy
 result.errors=log.errors;console.log(JSON.stringify(result));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
'''
@unittest.skipUnless(playwright_core(),'playwright-core not installed')
class OldTabTests(unittest.TestCase):
 def test_old_tab_switches_windows_after_one_deploy_and_reloads_once_after_two(self):
  with tempfile.TemporaryDirectory() as tmp:
   tmp=pathlib.Path(tmp);site=checkout(tmp);(site/'index.html').write_text((ROOT/'index.html').read_text());(site/'app.js').write_text((ROOT/'app.js').read_text());(site/'style.css').write_text((ROOT/'style.css').read_text())
   publish(tmp,prior(5),name='gen0')
   with patch.object(w.time,'sleep',lambda s:None):
    for i,name in enumerate(('gen1','gen2','gen3'),1):
     w.stage_site(site,tmp/name,'refresh',client_factory=Provider(5+i).factory,created_dates=lambda data:{'requested':0},published=LocalSite(tmp/('gen%d'%(i-1))))
   shutil.copytree(tmp/'gen3',tmp/'broken');(tmp/'broken'/w.read_site_data(LocalSite(tmp/'broken'))[1]['data']['month']).unlink()  # a deploy missing its own month file
   state={'root':tmp/'gen1'}
   class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*a):pass
    def translate_path(self,path):return str(state['root']/path.split('?')[0].lstrip('/')) if not path.startswith('/__deploy/') else ''
    def do_GET(self):
     if self.path.startswith('/__deploy/'):state['root']=tmp/self.path.rsplit('/',1)[1];self.send_response(204);self.end_headers();return
     del self.headers['If-Modified-Since']  # every generation is built within the same second; Pages revalidates by content ETag
     super().do_GET()
   server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
   proc=subprocess.run(['node','-e',DRIVER,playwright_core(),f'http://127.0.0.1:{server.server_address[1]}/'],capture_output=True,text=True,timeout=120)
   self.assertEqual(proc.returncode,0,proc.stderr);r=json.loads(proc.stdout);print('\nold-tab browser run:',json.dumps(r))
   self.assertEqual(r['opened'],{'count':'6 repositories','error':None,'period':'week'})
   # One deploy later: gen2 still carries gen1's files, the old tab's month request just works, no reload.
   self.assertEqual(r['one_deploy_later'],{'state':{'count':'6 repositories','error':None,'period':'month'},'reloads':0,'requests':1})
   # Two deploys later: gen1's day file is gone; the tab reloads index.html once and shows gen3's rows.
   self.assertEqual(r['two_deploys_later']['reloads'],1);self.assertEqual(r['two_deploys_later']['state']['count'],'8 repositories');self.assertIsNone(r['two_deploys_later']['state']['error'])
   # A deploy whose own month file is missing: at most one reload for that name, then the error, never a loop.
   self.assertLessEqual(r['broken_deploy']['reloads'],1);self.assertIn('Data request failed (404)',r['broken_deploy']['state']['error'])
   self.assertEqual(r['errors'],[])
