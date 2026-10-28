"""Build cache-safe static assets: one hashed data file per window, the app referencing them by hash, hashed CSS, index.html."""
import argparse,hashlib,pathlib
PERIODS=('day','week','month')
p=argparse.ArgumentParser();p.add_argument('--source',default='docs');p.add_argument('--output',required=True);a=p.parse_args();src=pathlib.Path(a.source);out=pathlib.Path(a.output);out.mkdir(parents=True,exist_ok=True)
def asset(stem,ext,data):
 name=f'{stem}.{hashlib.sha256(data).hexdigest()[:16]}.{ext}';(out/name).write_bytes(data);return name
js=(src/'app.js').read_text();data={}
for period in PERIODS:
 data[period]=asset(f'data-{period}','json',(src/f'data-{period}.json').read_bytes());assert f"'data-{period}.json'" in js;js=js.replace(f"'data-{period}.json'",f"'{data[period]}'")
app=asset('app','js',js.encode());css=asset('style','css',(src/'style.css').read_bytes())
html=(src/'index.html').read_text().replace('href="style.css"',f'href="{css}"').replace('src="app.js"',f'src="{app}"');(out/'index.html').write_text(html);(out/'.nojekyll').touch();print({'app':app,'css':css,'data':data})

for name,target in [('llms.txt',out/'llms.txt'),('agent-feed.md',out/'docs/agent-feed.md')]:
 if (src/name).is_file():
  target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((src/name).read_bytes())
