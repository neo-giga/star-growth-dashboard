"""The page's fonts: the vendored Geist woff2 files and their OFL licence are in the repository, and style.css embeds exactly those bytes."""
import base64,pathlib,re,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class FontTests(unittest.TestCase):
 def test_style_css_embeds_the_vendored_fonts_under_the_ofl(self):
  css=(ROOT/'style.css').read_text()
  embedded=dict(re.findall(r"@font-face\{font-family:'([^']+)'.*?src:url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\)",css))
  self.assertEqual(sorted(embedded),['Geist Mono Variable','Geist Variable'])
  self.assertEqual(base64.b64decode(embedded['Geist Variable']),(ROOT/'fonts/geist-latin-wght-normal.woff2').read_bytes())
  self.assertEqual(base64.b64decode(embedded['Geist Mono Variable']),(ROOT/'fonts/geist-mono-latin-wght-normal.woff2').read_bytes())
  licence=(ROOT/'fonts/OFL.txt').read_text()
  self.assertIn('SIL OPEN FONT LICENSE Version 1.1',licence);self.assertIn('The Geist Project Authors',licence);self.assertIn('SIL Open Font License 1.1 (fonts/OFL.txt)',css)
  self.assertNotRegex(css,r'url\((https?:)?//')  # no font CDN
if __name__=='__main__':
 unittest.main()
