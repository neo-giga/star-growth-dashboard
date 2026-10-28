"""The previous publication comes from the newest successful run's github-pages artifact, never from the Pages CDN.

A local HTTP server plays the GitHub REST API (runs, artifacts, the zip redirect), the pre-signed storage URL (which refuses the
GitHub token) and a stale CDN copy of the site; the real urllib/zip/tar path is exercised end to end.
"""
import contextlib,http.server,io,json,os,pathlib,shutil,sys,tarfile,tempfile,threading,unittest,urllib.parse,zipfile
from unittest.mock import patch
from trending.agent_feed import build_agent_exports
from trending.published import LocalSite,extract_pages_artifact,latest_publication
from test_weekly_refresh import ROOT,checkout,current,publish,w
OLD_APP='app.844cb05d8617d41a.js'  # the app the 2026-10-28 publication (run 40710274815) serves: one data.<hash>.json for all windows
def old_format_site(tmp,data,name='old-format'):
 """A site in the format of run 40710274815's artifact: index.html -> the tracked old app -> one data file with every window."""
 root=pathlib.Path(tmp)/name;root.mkdir();catalog=build_agent_exports(data,root);data=dict(data,agent_snapshot_id=catalog['snapshot_id'])
 shutil.copyfile(ROOT/OLD_APP,root/OLD_APP);(root/'index.html').write_text(f'<link rel="stylesheet" href="style.css"><script src="{OLD_APP}" defer></script>')
 (root/'data.7f31053344a74c7f.json').write_text(json.dumps(data));(root/'data.json').write_text(json.dumps(data));return root
def pages_zip(root):
 """The bytes GitHub serves for a github-pages artifact: a zip holding artifact.tar, members prefixed ./ like actions/upload-pages-artifact."""
 tar=io.BytesIO()
 with tarfile.open(fileobj=tar,mode='w') as t:t.add(root,arcname='.')
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w') as z:z.writestr('artifact.tar',tar.getvalue())
 return out.getvalue()
def run(rid,started,event='schedule'):
 return {'id':rid,'event':event,'head_sha':f'{rid:040x}','created_at':started,'run_started_at':started,'conclusion':'success'}
class FakeGitHub:
 def __init__(self,runs,artifacts,zips,cdn=None):
  self.runs,self.artifacts,self.zips,self.cdn=runs,artifacts,zips,cdn;self.hits=[]
  outer=self
  class Handler(http.server.BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def send(self,code,body=b'',headers=()):
    self.send_response(code)
    for k,v in headers:self.send_header(k,v)
    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
   def do_GET(self):
    url=urllib.parse.urlsplit(self.path);q=dict(urllib.parse.parse_qsl(url.query));parts=url.path.strip('/').split('/');outer.hits.append(url.path)
    if parts[0]=='api':
     if self.headers.get('Authorization')!='Bearer tok':return self.send(401)
     if parts[1:7]==['repos','o','r','actions','workflows','weekly-refresh.yml'] and parts[7]=='runs':
      assert (q['status'],q['branch'])==('success','main'),q;return self.send(200,json.dumps({'workflow_runs':outer.runs}).encode())
     if parts[1:6]==['repos','o','r','actions','runs'] and parts[7]=='artifacts':
      return self.send(200,json.dumps({'artifacts':[a for a in outer.artifacts.get(int(parts[6]),[]) if a['name']==q.get('name')]}).encode())
     if parts[1:6]==['repos','o','r','actions','artifacts'] and parts[7]=='zip':
      return self.send(302,headers=[('Location',f'http://127.0.0.1:{outer.port}/blob/{parts[6]}?sig=x')])
    if parts[0]=='blob':
     if self.headers.get('Authorization'):return self.send(400,b'token sent to storage')
     return self.send(200,outer.zips[int(parts[1])])
    if parts[0]=='cdn' and outer.cdn:
     path=pathlib.Path(outer.cdn,*parts[1:])
     return self.send(200,path.read_bytes()) if path.is_file() else self.send(404)
    self.send(404)
  self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.port=self.server.server_address[1]
  threading.Thread(target=self.server.serve_forever,daemon=True).start()
 def close(self):self.server.shutdown();self.server.server_close()
 def url(self,path):return f'http://127.0.0.1:{self.port}/{path}'
 def count(self,prefix):return sum(h.startswith(prefix) for h in self.hits)
def artifact(aid,expired=False):
 return {'id':aid,'name':'github-pages','expired':expired,'created_at':'2026-10-28T00:00:00Z','archive_download_url':f'API/repos/o/r/actions/artifacts/{aid}/zip'}
class ArtifactPublicationTests(unittest.TestCase):
 def serve(self,runs,artifacts,zips,cdn=None):
  gh=FakeGitHub(runs,{rid:[dict(a,archive_download_url=a['archive_download_url'].replace('API',f'http://127.0.0.1:{{}}/api')) for a in arts] for rid,arts in artifacts.items()},zips,cdn)
  for arts in gh.artifacts.values():
   for a in arts:a['archive_download_url']=a['archive_download_url'].format(gh.port)
  self.addCleanup(gh.close);return gh
 def cli(self,gh,tmp,*args,actions=True):
  """w.main() in-process with the Actions environment pointing at the fake API, LIVE_SITE at the fake (stale) CDN; returns (code, diagnostic)."""
  out=pathlib.Path(tmp)/'output';diag=pathlib.Path(tmp)/'diag.json';site=checkout(tmp,name='co') if not (pathlib.Path(tmp)/'co').exists() else pathlib.Path(tmp)/'co'
  shutil.rmtree(out,ignore_errors=True)
  env={'GITHUB_REPOSITORY':'o/r','GH_TOKEN':'tok','GITHUB_API_URL':gh.url('api'),'RUNNER_TEMP':str(tmp)}
  with patch.dict(os.environ,env),patch.object(w,'LIVE_SITE',gh.url('cdn/')),patch.object(sys,'argv',['weekly_refresh.py','--site-dir',str(site),'--output-dir',str(out),'--mode','validate-current','--diagnostic',str(diag),*args]),contextlib.redirect_stdout(io.StringIO()):
   if actions:os.environ['GITHUB_ACTIONS']='true'
   else:os.environ.pop('GITHUB_ACTIONS',None)
   code=w.main()
  return code,json.loads(diag.read_text())
 def test_the_old_single_file_artifact_of_run_40710274815_is_readable_and_re_released(self):
  with tempfile.TemporaryDirectory() as tmp:
   old=old_format_site(tmp,current());gh=self.serve([run(40710274815,'2026-10-28T14:52:39Z','workflow_dispatch')],{40710274815:[artifact(12825067811)]},{12825067811:pages_zip(old)})
   site=latest_publication('o/r','tok',pathlib.Path(tmp)/'dl',api=gh.url('api'))
   self.assertEqual(str(site),'artifact:run 40710274815 (workflow_dispatch, 0000000, started 2026-10-28T14:52:39Z)');self.assertEqual(gh.count('/blob/'),1)
   out=pathlib.Path(tmp)/'output';result=w.stage_site(checkout(tmp),out,'validate-current',client_factory=lambda **kw:self.fail('network'),published=site)
   self.assertEqual(result['snapshot_id'],json.loads((old/'api/v1/catalog.json').read_text())['snapshot_id']);self.assertEqual(result['previous_published'],str(site))
   self.assertEqual([len(f['rows']) for f in w.read_current(out)['feeds']],[1,1,1]);self.assertEqual((out/'api/v1/catalog.json').read_bytes(),(old/'api/v1/catalog.json').read_bytes())
 def test_actions_runs_read_the_newest_runs_artifact_never_the_stale_cdn(self):
  # Codex full-lists review #2: after a refresh deploys snapshot B, the CDN may still serve a coherent snapshot A; a validate-current
  # reading it would re-publish A. The previous publication now comes from the newest successful run's artifact.
  with tempfile.TemporaryDirectory() as tmp:
   older=publish(tmp,current(),name='older');newer_data=current()
   for f in newer_data['feeds']:f['rows'][0]['stars']=200;f['fetched_at_end']='2026-10-29T00:00:00Z'
   newer=publish(tmp,newer_data,name='newer')
   gh=self.serve([run(1,'2026-10-28T07:00:00Z'),run(2,'2026-10-29T07:00:00Z')],{1:[artifact(11)],2:[artifact(22)]},{11:pages_zip(older),22:pages_zip(newer)},cdn=older)
   code,diag=self.cli(gh,tmp)
   self.assertEqual(code,0,diag);self.assertTrue(diag['previous_published'].startswith('artifact:run 2 '),diag['previous_published'])
   self.assertEqual(diag['snapshot_id'],w.read_current(newer)['agent_snapshot_id']);self.assertNotEqual(diag['snapshot_id'],w.read_current(older)['agent_snapshot_id'])
   self.assertEqual(gh.count('/cdn/'),0);self.assertEqual(gh.hits.count('/blob/22'),1);self.assertEqual(gh.count('/blob/11'),0)
 def test_newest_run_without_a_readable_artifact_fails_closed_unless_bootstrapping_from_live(self):
  with tempfile.TemporaryDirectory() as tmp:
   pub=publish(tmp,current())
   gh=self.serve([run(1,'2026-10-28T07:00:00Z'),run(2,'2026-10-29T07:00:00Z')],{1:[artifact(11)],2:[artifact(22,expired=True)]},{11:pages_zip(pub)},cdn=pub)
   code,diag=self.cli(gh,tmp)
   self.assertEqual(code,2);self.assertTrue(diag['current_site_preserved']);self.assertFalse((pathlib.Path(tmp)/'output').exists())
   self.assertIn('publication artifact unreadable: ValueError: newest successful run 2 has no unexpired github-pages artifact; current site unchanged',diag['error'])
   self.assertEqual(gh.count('/blob/'),0);self.assertEqual(gh.count('/cdn/'),0)  # an older run's artifact would roll the site back
   code,diag=self.cli(gh,tmp,'--bootstrap-from-live')
   self.assertEqual(code,0,diag);self.assertEqual(diag['previous_published'],gh.url('cdn/'));self.assertGreater(gh.count('/cdn/'),0)
   self.assertTrue(diag['notes'][0].startswith('bootstrap_from_live: publication artifact unreadable: ValueError: newest successful run 2'),diag['notes'])
 def test_outside_actions_an_explicit_source_is_required(self):
  with tempfile.TemporaryDirectory() as tmp:
   pub=publish(tmp,current());gh=self.serve([],{},{},cdn=pub)
   code,diag=self.cli(gh,tmp,actions=False)
   self.assertEqual(code,2);self.assertIn('outside GitHub Actions pass --published-dir',diag['error']);self.assertEqual(gh.count('/cdn/'),0)
   code,diag=self.cli(gh,tmp,'--published-dir',str(pub),actions=False)
   self.assertEqual(code,0,diag);self.assertEqual(diag['previous_published'],f'dir:{pub.resolve()}');self.assertEqual(gh.count('/cdn/'),0)
   code,diag=self.cli(gh,tmp,'--published-site',gh.url('cdn/'),actions=False)
   self.assertEqual(code,0,diag);self.assertGreater(gh.count('/cdn/'),0)
 def test_artifact_members_may_not_leave_the_target(self):
  with tempfile.TemporaryDirectory() as tmp:
   tar=io.BytesIO()
   with tarfile.open(fileobj=tar,mode='w') as t:
    info=tarfile.TarInfo('../escape.txt');info.size=1;t.addfile(info,io.BytesIO(b'x'))
   path=pathlib.Path(tmp)/'a.zip'
   with zipfile.ZipFile(path,'w') as z:z.writestr('artifact.tar',tar.getvalue())
   with self.assertRaises(tarfile.TarError):extract_pages_artifact(path,pathlib.Path(tmp)/'site')
   self.assertFalse((pathlib.Path(tmp)/'escape.txt').exists())
