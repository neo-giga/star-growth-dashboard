"""Build cache-safe static assets: the hashed data file, the app referencing it by hash, hashed CSS, index.html."""
import argparse,hashlib,pathlib
p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);a=p.parse_args();src=pathlib.Path(a.source);out=pathlib.Path(a.output);out.mkdir(parents=True,exist_ok=True)
def asset(stem,ext,data):
 name=f'{stem}.{hashlib.sha256(data).hexdigest()[:16]}.{ext}';(out/name).write_bytes(data);return name
js=(src/'app.js').read_text()
data=asset('data','json',(src/'data.json').read_bytes());assert "'data.json'" in js;js=js.replace("'data.json'",f"'{data}'")
app=asset('app','js',js.encode());css=asset('style','css',(src/'style.css').read_bytes())
html=(src/'index.html').read_text().replace('href="style.css"',f'href="{css}"').replace('src="app.js"',f'src="{app}"');(out/'index.html').write_text(html);(out/'.nojekyll').touch();print({'app':app,'css':css,'data':data})
