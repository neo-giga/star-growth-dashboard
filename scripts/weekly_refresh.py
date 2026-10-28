"""Stage a complete refresh without mutating the currently published site.

validate-current exercises export/deployment plumbing, not new data collection.
Refresh mode reads the provider's top N rows per period (--top, default 2000) twice, past the
provider's whole advertised cache window, and publishes only when both reads agree; --top 0 is the
strict whole-feed rule.
Public cached GitHub metadata is carried only for same period/provider ID/name.
"""
from __future__ import annotations
import argparse, copy, datetime as dt, json, os, pathlib, re, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from trending.feeds import FeedClient, collect_feed
from fill_created_dates import fill_created_dates
# GitTrend answers with Cache-Control: s-maxage=300, stale-while-revalidate=600, so a shared cache may replay a response
# for up to 900 s. Every later pass starts after that whole window plus a margin, so it cannot be served pass A's pages.
RETRY_DELAY = 300 + 600 + 10


def local_asset(root, name):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name.startswith('.'):
        raise ValueError('unsafe asset reference')
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing/nonregular asset')
    return path


def read_current(site):
    site = pathlib.Path(site)
    index = local_asset(site, 'index.html').read_text()
    app_name = re.search(r'<script[^>]+src="([^"]+)"', index)
    css_name = re.search(r'<link[^>]+href="([^"]+\.css)"', index)
    if not app_name or not css_name:
        raise ValueError('missing UI assets')
    app = local_asset(site, app_name[1]).read_text()
    css = local_asset(site, css_name[1]).read_text()
    data_name = re.search(r"fetch\('([^']+)'", app)
    if not data_name:
        raise ValueError('missing local data reference')
    data = json.loads(local_asset(site, data_name[1]).read_text())
    validate_dashboard(data)
    ui = {'index.html': index.replace(app_name[1], 'app.js').replace(css_name[1], 'style.css'),
          'app.js': app.replace(data_name[1], 'data.json'), 'style.css': css}
    return data, ui


def validate_dashboard(data):
    if not isinstance(data, dict) or not isinstance(data.get('feeds'), list):
        raise ValueError('invalid dashboard')
    periods = set()
    for feed in data['feeds']:
        period = feed.get('period')
        if feed.get('provider') != 'gittrend' or period not in ('day', 'week', 'month') or period in periods:
            raise ValueError('foreign/duplicate feed')
        periods.add(period)
        if not isinstance(feed.get('rows'), list):
            raise ValueError('invalid rows')
        ids = set()
        for row in feed['rows']:
            if type(row.get('repo_id')) is not int or row['repo_id'] in ids or not isinstance(row.get('full_name'), str):
                raise ValueError('invalid/duplicate provider identity')
            ids.add(row['repo_id'])
    if periods != {'day','week','month'}:
        raise ValueError('all three separate periods required')


def bounded_problem(result, top):
    """Why a top-N collection is not publishable, or None: every page full except a genuine last page, no duplicate IDs,
    min(top, provider total) rows. A provider total that moves between pages stays in coverage warnings but is allowed."""
    coverage, pages, limit = result['coverage'], result['pages'], result['coverage']['limit']
    if coverage['failure']:
        return coverage['failure']
    if coverage['duplicates']:
        return f'{coverage["duplicates"]} duplicate provider IDs'
    for index, page in enumerate(pages):
        expected = page['total'] - (page['page'] - 1) * limit if coverage['reached_end'] and index == len(pages) - 1 else limit
        if page['returned'] != expected:
            return f'page {page["page"]} returned {page["returned"]}, expected {expected}'
    expected = min(top, coverage['last_reported_total']) if coverage['reached_end'] else top
    if len(result['rows']) != expected:
        return f'{len(result["rows"])} rows, expected {expected}'
    return None


def stability_problem(earlier, later, top):
    """Why two reads of a period's pages do not establish its top-N membership, or None. Full, duplicate-free pages cannot
    show that a repository which changed rank between two page reads was not skipped; two passes with the identical
    ordered ID list can."""
    for label, result in (('earlier', earlier), ('later', later)):
        problem = bounded_problem(result, top)
        if problem:
            return f'{label} pass: {problem}'
    if [r['repo_id'] for r in earlier['rows']] != [r['repo_id'] for r in later['rows']]:
        return 'membership or order changed between passes'
    return None


def read_period(client_factory, period, cache, max_pages):
    client = client_factory(cache_dir=cache, resume=False)
    if client_factory is FeedClient:
        fetch = client.fetch
        last = [None]
        def paced_fetch(url):
            if last[0] is not None:
                time.sleep(max(0.0, 1.0 - (time.monotonic() - last[0])))
            last[0] = time.monotonic()
            return fetch(url)
        client.fetch = paced_fetch
    return collect_feed(client, period, max_pages=max_pages)


def collect_strict(client_factory, max_pages, checkpoint_dir):
    collected = {}
    for period in ('day','week','month'):
        cache = pathlib.Path(checkpoint_dir) / period if checkpoint_dir else None
        result = read_period(client_factory, period, cache, max_pages)
        coverage = result['coverage']
        if (coverage['status'] != 'complete' or not coverage['reached_end'] or
                coverage['unique_repos'] != coverage['reported_total'] or
                coverage['last_reported_total'] != coverage['reported_total'] or coverage['duplicates']):
            raise ValueError(f'{period} refresh not publishable: {coverage["status"]}; current site unchanged')
        collected[period] = result
    return collected


def collect_bounded(client_factory, top, checkpoint_dir, sleeper):
    """Pass A, one RETRY_DELAY wait (past the provider's cache window), pass B of the same pages: a period publishes its later pass when
    both passes satisfy bounded_problem with the identical ordered IDs. Otherwise one more wait and a pass C for the
    unstable periods, accepted only when it agrees with B the same way."""
    wait = sleeper or time.sleep
    passes = {period: [] for period in ('day','week','month')}
    for suffix in ('', '-verify'):
        if suffix:
            wait(RETRY_DELAY)
        for period in passes:
            cache = pathlib.Path(checkpoint_dir) / (period + suffix) if checkpoint_dir else None
            passes[period].append(read_period(client_factory, period, cache, top // 100))
    unstable = [period for period in passes if stability_problem(*passes[period][-2:], top)]
    if unstable:
        wait(RETRY_DELAY)
        for period in unstable:
            cache = pathlib.Path(checkpoint_dir) / f'{period}-retry' if checkpoint_dir else None
            passes[period].append(read_period(client_factory, period, cache, top // 100))
    collected = {}
    for period, runs in passes.items():
        problem = stability_problem(*runs[-2:], top)
        if problem:
            raise ValueError(f'{period} refresh not publishable: {problem}; current site unchanged')
        runs[-1]['coverage']['verified_passes'] = ['A', 'B'] if len(runs) == 2 else ['B', 'C']
        collected[period] = runs[-1]
    return collected


def refresh_dashboard(current, client_factory=FeedClient, max_pages=1000, checkpoint_dir=None, top=2000, sleeper=None):
    validate_dashboard(current)
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise ValueError('max_pages must be 1..1000')
    if type(top) is not int or (top and not (100 <= top <= 10000 and top % 100 == 0)):
        raise ValueError('top must be 0 (whole feed, strict) or a multiple of 100 in 100..10000')
    if top > max_pages * 100:
        raise ValueError('max_pages bound is below the pages top needs')
    results = []
    collected = collect_bounded(client_factory, top, checkpoint_dir, sleeper) if top else collect_strict(client_factory, max_pages, checkpoint_dir)
    for period in ('day','week','month'):
        result = collected[period]
        coverage = result['coverage']
        prior_feed = next(f for f in current['feeds'] if f['period'] == period)
        if top:
            # Shrink guard: a valid-looking feed that lost over half of min(top, previous rows) is a broken provider response.
            if len(result['rows']) < min(top, len(prior_feed['rows'])) / 2:
                raise ValueError(f'{period} refresh not publishable: {len(result["rows"])} rows is under half of min(top, previous {len(prior_feed["rows"])}); current site unchanged')
            coverage['bounded_top'] = top
        prior = {(r['repo_id'],r['full_name'].casefold()):r for r in prior_feed['rows']}
        rows = []
        for row in result['rows']:
            clean = {k: row.get(k) for k in ('repo_id','full_name','stars','gain','language','observation')}
            old = prior.get((row['repo_id'],row['full_name'].casefold()), {})
            clean.update({k:copy.deepcopy(old.get(k)) for k in ('created_at','created_at_github_repo_id','metadata')})
            rows.append(clean)
        feed = dict(provider='gittrend',period=period,source_as_of=None,
                    fetched_at_start=result['fetched_started_at'],fetched_at_end=result['fetched_at'],
                    coverage=coverage,rows=rows)
        for key in ('creation_dates','metadata_coverage'):
            if key in prior_feed:
                feed[key] = copy.deepcopy(prior_feed[key])
        # Counts must reflect this refresh; source observation times remain unchanged.
        if 'creation_dates' in feed:
            known = sum(r.get('created_at') is not None for r in rows)
            feed['creation_dates'].update(known=known,unknown=len(rows)-known)
        if 'metadata_coverage' in feed:
            known = {field:sum(isinstance(r.get('metadata'),dict) and r['metadata'].get(field) is not None for r in rows) for field in feed['metadata_coverage'].get('known_by_field',{})}
            matched = sum(isinstance(r.get('metadata'),dict) and r['metadata'].get('github_repo_id') is not None for r in rows)
            feed['metadata_coverage'].update(matched_names=matched,unmatched_names=len(rows)-matched,known_by_field=known,unknown_by_field={k:len(rows)-v for k,v in known.items()})
            feed['metadata_coverage']['refresh_note'] = 'Carried cached metadata only for unchanged provider ID and name; original observation dates retained.'
        results.append(feed)
    return {'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'feeds':results}


def copy_public_site(site, stage):
    for path in pathlib.Path(site).iterdir():
        if path.is_symlink():
            raise ValueError('symlinks are not publishable')
        if path.is_file() and (path.name in ('index.html','.nojekyll','llms.txt') or re.fullmatch(r'(?:app|style|data)(?:\.[0-9a-f]+)?\.(?:js|css|json)',path.name)):
            shutil.copyfile(path, stage/path.name)
        elif path.is_dir() and path.name == 'api':
            if any(p.is_symlink() for p in path.rglob('*')):
                raise ValueError('symlink in API tree')
            shutil.copytree(path,stage/path.name)
        elif path.is_dir() and path.name == 'docs':
            (stage/'docs').mkdir()
            for name in ('agent-feed.md','weekly-refresh.yml'):
                doc=path/name
                if doc.is_file() and not doc.is_symlink(): shutil.copyfile(doc,stage/'docs'/name)


def stage_site(site_dir, output_dir, mode='refresh', max_pages=1000, checkpoint_dir=None,
               client_factory=FeedClient, export_builder=None, created_dates=fill_created_dates, top=2000):
    site, output = pathlib.Path(site_dir).resolve(), pathlib.Path(output_dir).resolve()
    if output.exists() or output == site or site in output.parents:
        raise ValueError('output must be a new directory outside current site')
    if mode not in ('refresh','validate-current'):
        raise ValueError('invalid mode')
    current, ui = read_current(site)
    from trending.agent_feed import hydrate_ui_index, build_ui_index
    current = hydrate_ui_index(current, site)
    data = refresh_dashboard(current,client_factory,max_pages,checkpoint_dir,top) if mode == 'refresh' else copy.deepcopy(current)
    # New rows arrive without a GitHub creation date; ask GitHub (via gh) before publishing. Failures leave UNKNOWN.
    created = created_dates(data) if mode == 'refresh' else None
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.refresh-',dir=output.parent) as temp:
        temp=pathlib.Path(temp);stage=temp/'site';stage.mkdir();copy_public_site(site,stage)
        if export_builder is None:
            from trending.agent_feed import build_agent_exports
            export_builder=build_agent_exports
        catalog = export_builder(data,stage)
        compact = build_ui_index(data,catalog)
        source=temp/'ui';source.mkdir()
        for name,value in ui.items(): (source/name).write_text(value)
        (source/'data.json').write_text(json.dumps(compact,separators=(',',':'))+'\n')
        subprocess.run([sys.executable,str(pathlib.Path(__file__).with_name('build_static_release.py')),
                        '--source',str(source),'--output',str(stage)],check=True,capture_output=True,text=True)
        os.replace(stage,output)
    return {'mode':mode,'new_retrieval':mode=='refresh','top':top if mode == 'refresh' else None,'output':str(output),'created_dates':created,
            'periods':[{ 'period':f['period'],'rows':len(f['rows']),'status':f['coverage']['status'],
                        'verified_passes':f['coverage'].get('verified_passes'),'fetched_at_end':f['fetched_at_end']} for f in data['feeds']]}


def main():
    p=argparse.ArgumentParser();p.add_argument('--site-dir',required=True);p.add_argument('--output-dir',required=True)
    p.add_argument('--mode',choices=('refresh','validate-current'),default='refresh');p.add_argument('--max-pages',type=int,default=1000)
    p.add_argument('--top',type=int,default=2000,help='rows per period (multiple of 100, 100..10000); 0 reads the whole feed under the strict rule')
    p.add_argument('--checkpoint-dir');p.add_argument('--diagnostic');a=p.parse_args()
    try:
        result=stage_site(a.site_dir,a.output_dir,a.mode,a.max_pages,a.checkpoint_dir,top=a.top);code=0
    except Exception as exc:
        result={'mode':a.mode,'status':'failed','error':f'{type(exc).__name__}: {str(exc)[:500]}','current_site_preserved':True};code=2
    if a.diagnostic:
        path=pathlib.Path(a.diagnostic);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2));return code
if __name__=='__main__':raise SystemExit(main())
