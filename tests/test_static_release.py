import pathlib,subprocess,sys,tempfile,unittest,re
SCRIPT=pathlib.Path(__file__).resolve().parents[1]/'scripts/build_static_release.py'
class StaticReleaseTests(unittest.TestCase):
 def test_immutable_assets_link_as_one_release_and_retain_old(self):
  with tempfile.TemporaryDirectory() as tmp:
   src=pathlib.Path(tmp)/'src';out=pathlib.Path(tmp)/'out';src.mkdir()
   (src/'index.html').write_text('<link href="style.css"><script src="app.js"></script>')
   (src/'style.css').write_text('body{}');(src/'app.js').write_text("const DATA_FILES = {day: 'data-day.json', week: 'data-week.json', month: 'data-month.json'};")
   for period in ('day','week','month'):(src/f'data-{period}.json').write_text('{"version":1,"period":"%s"}'%period)
   def build():
    subprocess.run([sys.executable,str(SCRIPT),'--source',str(src),'--output',str(out)],check=True,capture_output=True)
    html=(out/'index.html').read_text();app=re.search(r'src="([^"]+)"',html)[1];css=re.search(r'href="([^"]+)"',html)[1]
    self.assertTrue((out/css).exists());js=(out/app).read_text();self.assertNotIn("'data-day.json'",js)
    data={m[2]:m[1] for m in re.finditer(r"'(data-(day|week|month)\.[0-9a-f]{16}\.json)'",js)};self.assertEqual(sorted(data),['day','month','week'])
    for name in data.values():self.assertTrue((out/name).exists())
    return app,data
   first=build();self.assertEqual(first,build());(src/'data-week.json').write_text('{"version":2,"period":"week"}');second=build()
   self.assertNotEqual(first[0],second[0]);self.assertNotEqual(first[1]['week'],second[1]['week']);self.assertEqual(first[1]['day'],second[1]['day'])
   self.assertTrue(all((out/f).exists() for f in [first[0],*first[1].values()]));self.assertTrue((out/'.nojekyll').exists())
 def test_missing_window_file_fails_the_build(self):
  with tempfile.TemporaryDirectory() as tmp:
   src=pathlib.Path(tmp)/'src';out=pathlib.Path(tmp)/'out';src.mkdir()
   (src/'index.html').write_text('<link href="style.css"><script src="app.js"></script>');(src/'style.css').write_text('body{}');(src/'app.js').write_text("'data-day.json' 'data-week.json' 'data-month.json'")
   for period in ('day','week'):(src/f'data-{period}.json').write_text('{}')
   proc=subprocess.run([sys.executable,str(SCRIPT),'--source',str(src),'--output',str(out)],capture_output=True,text=True);self.assertNotEqual(proc.returncode,0);self.assertFalse((out/'index.html').exists())
