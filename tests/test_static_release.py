import pathlib,subprocess,sys,tempfile,unittest,re
SCRIPT=pathlib.Path(__file__).resolve().parents[1]/'scripts/build_static_release.py'
class StaticReleaseTests(unittest.TestCase):
 def test_immutable_assets_link_as_one_release_and_retain_old(self):
  with tempfile.TemporaryDirectory() as tmp:
   src=pathlib.Path(tmp)/'src';out=pathlib.Path(tmp)/'out';src.mkdir()
   (src/'index.html').write_text('<link href="style.css"><script src="app.js"></script>')
   (src/'style.css').write_text('body{}');(src/'app.js').write_text("fetch('data.json', {cache:'no-store'})");(src/'data.json').write_text('{"version":1}')
   def build():
    subprocess.run([sys.executable,str(SCRIPT),'--source',str(src),'--output',str(out)],check=True,capture_output=True)
    html=(out/'index.html').read_text();app=re.search(r'src="([^"]+)"',html)[1];css=re.search(r'href="([^"]+)"',html)[1]
    self.assertTrue((out/css).exists());data=re.search(r"fetch\('([^']+)'",(out/app).read_text())[1];self.assertTrue((out/data).exists());return app,data
   first=build();self.assertEqual(first,build());(src/'data.json').write_text('{"version":2}');second=build();self.assertNotEqual(first[0],second[0]);self.assertNotEqual(first[1],second[1]);self.assertTrue(all((out/f).exists() for f in first));self.assertTrue((out/'.nojekyll').exists())
