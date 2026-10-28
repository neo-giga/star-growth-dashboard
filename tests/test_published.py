"""Reading the previously published state: the live site's references, the checkout's data files, path safety."""
import json,os,pathlib,tempfile,unittest
from trending.published import LiveSite,LocalSite,checkout_data_files,data_references,read_site_data,seed_created_dates
def feed(period,rows):
 return {'provider':'gittrend','period':period,'source_as_of':None,'fetched_at_start':'2026-01-01T00:00:00Z','fetched_at_end':'2026-01-01T00:00:00Z','coverage':{'status':'partial'},'rows':rows}
def row(i,created=None):
 return {'repo_id':i,'full_name':f'o/r{i}','stars':1,'gain':1,'language':None,'created_at':created,'created_at_github_repo_id':7 if created else None}
class ReferenceTests(unittest.TestCase):
 def test_per_window_and_single_file_apps(self):
  self.assertEqual(data_references("const DATA_FILES = {day: 'data-day.0123456789abcdef.json', week: 'data-week.fedcba9876543210.json', month: 'data-month.0000000000000000.json'};"),
   {'day':'data-day.0123456789abcdef.json','week':'data-week.fedcba9876543210.json','month':'data-month.0000000000000000.json'})
  self.assertEqual(data_references("fetch('data.7f31053344a74c7f.json', {cache:'no-store'})"),{'all':'data.7f31053344a74c7f.json'})
  with self.assertRaises(ValueError):data_references("fetch('other.json')")
 def test_live_site_reads_index_app_and_window_files_and_rejects_bad_paths(self):
  files={'index.html':b'<script src="app.1111111111111111.js" defer></script>','app.1111111111111111.js':b"'data-day.aaaaaaaaaaaaaaaa.json' 'data-week.bbbbbbbbbbbbbbbb.json' 'data-month.cccccccccccccccc.json'"}
  for period,name in (('day','data-day.aaaaaaaaaaaaaaaa.json'),('week','data-week.bbbbbbbbbbbbbbbb.json'),('month','data-month.cccccccccccccccc.json')):
   files[name]=json.dumps({'generated_at':'2026-10-28T00:00:00Z','agent_snapshot_id':'s','feeds':[feed(period,[row(1)])]}).encode()
  urls=[];site=LiveSite('https://example.test/site',fetch=lambda url:(urls.append(url),files[url.removeprefix('https://example.test/site/')])[1])
  data,names=read_site_data(site);self.assertEqual([f['period'] for f in data['feeds']],['day','week','month']);self.assertEqual(data['agent_snapshot_id'],'s');self.assertEqual(names['app'],'app.1111111111111111.js')
  self.assertEqual(urls[0],'https://example.test/site/index.html');self.assertEqual(site.requests,5)
  for bad in ('../x','/etc/passwd','a//b','.hidden','a/../b'):
   with self.subTest(bad=bad),self.assertRaises(ValueError):site.read(bad)
  with self.assertRaises(ValueError):LiveSite('ftp://example.test/')
  files['data-week.bbbbbbbbbbbbbbbb.json']=json.dumps({'generated_at':'2025-01-01T00:00:00Z','agent_snapshot_id':'s','feeds':[feed('week',[row(1)])]}).encode()
  with self.assertRaisesRegex(ValueError,'different publications'):read_site_data(site)
 def test_local_site_never_leaves_its_root_or_follows_symlinks(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp)/'root';root.mkdir();(root/'index.html').write_text('x');(pathlib.Path(tmp)/'outside.txt').write_text('secret');os.symlink(pathlib.Path(tmp)/'outside.txt',root/'link.txt')
   site=LocalSite(root);self.assertEqual(site.read('index.html'),b'x')
   for bad in ('link.txt','../outside.txt','missing.html'):
    with self.subTest(bad=bad),self.assertRaises(ValueError):site.read(bad)
class CheckoutTests(unittest.TestCase):
 def test_newest_publication_first_grouping_per_window_files_and_seeds(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp)
   (root/'data.0000000000000000.json').write_text(json.dumps({'generated_at':'2026-10-20T00:00:00Z','feeds':[feed(p,[row(1,'2020-01-01T00:00:00Z'),row(2,'2019-01-01T00:00:00Z')]) for p in ('day','week','month')]}))
   (root/'data.json').write_text(json.dumps({'generated_at':'2026-10-20T00:00:00Z','feeds':[feed(p,[row(1,'2020-01-01T00:00:00Z')]) for p in ('day','week','month')]}))
   for p in ('day','week','month'):(root/f'data-{p}.1234567890abcdef.json').write_text(json.dumps({'generated_at':'2026-10-29T00:00:00Z','agent_snapshot_id':'s','feeds':[feed(p,[row(1,'2021-01-01T00:00:00Z'),row(3)])]}))
   (root/'data-day.ffffffffffffffff.json').write_text(json.dumps({'generated_at':'2026-10-30T00:00:00Z','feeds':[feed('day',[row(9)])]}))  # incomplete group: skipped
   (root/'data.bad0bad0bad0bad0.json').write_text('{not json');(root/'data.f0f0f0f0f0f0f0f0.json').write_text(json.dumps({'feeds':[feed('day',[])]}));(root/'data.old.json').write_text('{}')  # unreadable, foreign, not a release name: all skipped
   found=checkout_data_files(root)
   self.assertEqual([(g,n) for g,n,_ in found],[('2026-10-29T00:00:00Z',['data-day.1234567890abcdef.json','data-week.1234567890abcdef.json','data-month.1234567890abcdef.json']),('2026-10-20T00:00:00Z',['data.0000000000000000.json']),('2026-10-20T00:00:00Z',['data.json'])])
   seeds=seed_created_dates(found);self.assertEqual(seeds,{(1,'o/r1'):('2021-01-01T00:00:00Z',7),(2,'o/r2'):('2019-01-01T00:00:00Z',7)})
   self.assertEqual(checkout_data_files(root/'nothing-here') if (root/'nothing-here').mkdir() is None else None,[])
