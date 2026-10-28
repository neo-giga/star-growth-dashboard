"""Stage a complete refresh without mutating the currently published site.

validate-current exercises export/deployment plumbing, not new data collection.
Refresh mode reads each period in two tiers: the head (the first --verify-top rows, default 2000) is read
twice, past the provider's whole advertised cache window, and publishes only when both reads agree; the
tail (every later page up to --depth, default all) is read once, de-duplicated by provider ID, and must
reach the provider's end with at least TAIL_COVERAGE of the reported total. --top N is an alias for
--depth N --verify-top N (today's bounded behaviour); --top 0 is the strict whole-feed rule.
Public cached GitHub metadata is carried only for same period/provider ID/name.
"""
from __future__ import annotations
import argparse, copy, datetime as dt, json, math, os, pathlib, re, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from trending.feeds import FeedClient, collect_feed
from fill_created_dates import fill_created_dates
# GitTrend answers with Cache-Control: s-maxage=300, stale-while-revalidate=600, so a shared cache may replay a response
# for up to 900 s. Every later pass starts after that whole window plus a margin, so it cannot be served pass A's pages.
RETRY_DELAY = 300 + 600 + 10
# A once-read tail may lose rows to rank shifts between page reads; it is published only when the unique head+tail
# rows cover this share of the smaller of the first/last provider totals seen in the pass.
TAIL_COVERAGE = 0.98


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


def read_period(client_factory, period, cache, max_pages, first_page=1, keep_first=False):
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
    return collect_feed(client, period, max_pages=max_pages, first_page=first_page, keep_first=keep_first)


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


def merge_two_tier(period, head, first_head, tail, depth, verify_top, tail_bound):
    """The published period: the verified head (its latest agreeing pass) followed by pass A's once-read tail, or why the
    tail is not publishable (ValueError). Tail rows already in the head are dropped (the head rank is the best rank);
    a provider total that moves during the tail is tolerated and recorded, a fetch failure, a stop before the provider's
    end (or before the depth bound) or unique rows under TAIL_COVERAGE of the smaller first/last total are not."""
    coverage = copy.deepcopy(head['coverage'])
    rows = list(head['rows'])
    first_total = first_head['coverage']['reported_total']
    last_total = head['coverage']['last_reported_total']
    pages = list(head['pages'])
    starts, ends = [head['fetched_started_at']], [head['fetched_at']]
    tail_info = None
    if tail is not None:
        tc = tail['coverage']
        if tc['failure']:
            raise ValueError(f'{period} refresh not publishable: tail {tc["failure"]}; current site unchanged')
        if not tc['reached_end'] and (depth == 'all' or tc['pages_fetched'] < tail_bound):
            raise ValueError(f'{period} refresh not publishable: tail stopped after {tc["pages_fetched"]} pages before the provider end; current site unchanged')
        head_ids = {r['repo_id'] for r in rows}
        overlap = 0
        for row in tail['rows']:
            if row['repo_id'] in head_ids:
                overlap += 1
                continue
            rows.append(row)
        last_total = tc['last_reported_total']
        floor = min(first_total, last_total) if depth == 'all' else min(first_total, last_total, depth)
        required = math.ceil(TAIL_COVERAGE * floor)
        if len(rows) < required:
            raise ValueError(f'{period} refresh not publishable: {len(rows)} unique rows is under {TAIL_COVERAGE:.0%} of the {floor} reported; current site unchanged')
        tail_info = {'pages_fetched': tc['pages_fetched'], 'rows': len(rows) - len(head['rows']), 'duplicates': tc['duplicates'],
                     'overlap_with_verified_top': overlap, 'first_reported_total': first_total, 'last_reported_total': last_total,
                     'total_drift': last_total - first_total, 'required_rows': required, 'reached_end': tc['reached_end'],
                     'fetched_at_start': tail['fetched_started_at'], 'fetched_at_end': tail['fetched_at'], 'warnings': list(tc['warnings'])}
        pages += tail['pages']
        starts.append(tail['fetched_started_at']); ends.append(tail['fetched_at'])
        coverage['warnings'] = coverage['warnings'] + ['tail: ' + w for w in tc['warnings']]
        coverage['reached_end'] = tc['reached_end']
        coverage['max_pages'] = coverage['max_pages'] + tc['max_pages']
    coverage.update(pages_fetched=len(pages), reported_total=first_total, last_reported_total=last_total, unique_repos=len(rows),
                    depth=depth, verified_top=verify_top, verified_rows=len(head['rows']), tail=tail_info)
    coverage['status'] = 'inconsistent' if coverage['warnings'] else ('complete' if coverage['reached_end'] else 'partial')
    key = lambda t: dt.datetime.fromisoformat(t)
    return dict(head, coverage=coverage, rows=rows, pages=pages, fetched_started_at=min(starts, key=key), fetched_at=max(ends, key=key))


def collect_two_tier(client_factory, depth, verify_top, max_pages, checkpoint_dir, sleeper):
    """Pass A reads the whole period: the head (min(verify_top, depth) rows) under bounded_problem, then the tail (every
    later page up to depth or max_pages) once with duplicate IDs keeping their best rank. After one RETRY_DELAY wait
    (past the provider's cache window) pass B re-reads the head: a period publishes when both head passes satisfy
    bounded_problem with the identical ordered IDs, otherwise one more wait and a pass C for the unstable periods,
    accepted only when it agrees with B the same way. depth == verify_top is today's bounded behaviour (no tail)."""
    wait = sleeper or time.sleep
    head_pages = min(verify_top, depth if depth != 'all' else verify_top) // 100
    bound = max_pages if depth == 'all' else depth // 100
    periods = ('day', 'week', 'month')
    heads, tails = {period: [] for period in periods}, {}
    cache = lambda period, suffix: pathlib.Path(checkpoint_dir) / (period + suffix) if checkpoint_dir else None
    for period in periods:
        head = read_period(client_factory, period, cache(period, ''), head_pages)
        heads[period].append(head)
        if bound > head_pages and not head['coverage']['reached_end'] and not head['coverage']['failure']:
            tails[period] = read_period(client_factory, period, cache(period, '-tail'), bound - head_pages, first_page=head_pages + 1, keep_first=True)
    wait(RETRY_DELAY)
    for period in periods:
        heads[period].append(read_period(client_factory, period, cache(period, '-verify'), head_pages))
    unstable = [period for period in periods if stability_problem(*heads[period][-2:], head_pages * 100)]
    if unstable:
        wait(RETRY_DELAY)
        for period in unstable:
            heads[period].append(read_period(client_factory, period, cache(period, '-retry'), head_pages))
    collected = {}
    for period, runs in heads.items():
        problem = stability_problem(*runs[-2:], head_pages * 100)
        if problem:
            raise ValueError(f'{period} refresh not publishable: {problem}; current site unchanged')
        runs[-1]['coverage']['verified_passes'] = ['A', 'B'] if len(runs) == 2 else ['B', 'C']
        collected[period] = merge_two_tier(period, runs[-1], runs[0], tails.get(period), depth, head_pages * 100, bound - head_pages)
    return collected


def resolve_depth(top, depth, verify_top, max_pages):
    """(depth, verify_top) for the run, or (0, 0) for the strict whole-feed rule; --top N is --depth N --verify-top N."""
    if top is not None:
        if type(top) is not int or (top and not (100 <= top <= 10000 and top % 100 == 0)):
            raise ValueError('top must be 0 (whole feed, strict) or a multiple of 100 in 100..10000')
        if not top:
            return 0, 0
        depth = verify_top = top
    if type(verify_top) is not int or not (100 <= verify_top <= 10000 and verify_top % 100 == 0):
        raise ValueError('verify_top must be a multiple of 100 in 100..10000')
    if depth != 'all' and (type(depth) is not int or not (100 <= depth <= 100000 and depth % 100 == 0)):
        raise ValueError("depth must be 'all' or a multiple of 100 in 100..100000")
    if min(verify_top, depth if depth != 'all' else verify_top) > max_pages * 100:
        raise ValueError('max_pages bound is below the pages verify_top needs')
    return depth, verify_top


def refresh_dashboard(current, client_factory=FeedClient, max_pages=1000, checkpoint_dir=None, top=None, sleeper=None,
                      depth='all', verify_top=2000, seeds=None):
    """seeds: {(provider id, casefolded name): (created_at, github id)} from older published data for rows the previous
    published data (current) does not carry."""
    validate_dashboard(current)
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise ValueError('max_pages must be 1..1000')
    depth, verify_top = resolve_depth(top, depth, verify_top, max_pages)
    results = []
    collected = collect_two_tier(client_factory, depth, verify_top, max_pages, checkpoint_dir, sleeper) if verify_top else collect_strict(client_factory, max_pages, checkpoint_dir)
    for period in ('day','week','month'):
        result = collected[period]
        coverage = result['coverage']
        prior_feed = next(f for f in current['feeds'] if f['period'] == period)
        if verify_top:
            # Shrink guard: a valid-looking feed that lost over half of min(depth, previous rows) is a broken provider response.
            floor = len(prior_feed['rows']) if depth == 'all' else min(depth, len(prior_feed['rows']))
            if len(result['rows']) < floor / 2:
                raise ValueError(f'{period} refresh not publishable: {len(result["rows"])} rows is under half of min(depth, previous {len(prior_feed["rows"])}); current site unchanged')
            if depth != 'all':
                coverage['bounded_top'] = depth
        prior = {(r['repo_id'],r['full_name'].casefold()):r for r in prior_feed['rows']}
        rows = []
        for row in result['rows']:
            clean = {k: row.get(k) for k in ('repo_id','full_name','stars','gain','language','observation')}
            key = (row['repo_id'],row['full_name'].casefold())
            old = prior.get(key, {})
            clean.update({k:copy.deepcopy(old.get(k)) for k in ('created_at','created_at_github_repo_id','metadata')})
            if clean['created_at'] is None and seeds and key in seeds:
                clean['created_at'], clean['created_at_github_repo_id'] = seeds[key]
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
               client_factory=FeedClient, export_builder=None, created_dates=fill_created_dates, top=None, depth='all', verify_top=2000):
    site, output = pathlib.Path(site_dir).resolve(), pathlib.Path(output_dir).resolve()
    if output.exists() or output == site or site in output.parents:
        raise ValueError('output must be a new directory outside current site')
    if mode not in ('refresh','validate-current'):
        raise ValueError('invalid mode')
    current, ui = read_current(site)
    from trending.agent_feed import hydrate_ui_index, build_ui_index
    current = hydrate_ui_index(current, site)
    data = refresh_dashboard(current,client_factory,max_pages,checkpoint_dir,top,depth=depth,verify_top=verify_top) if mode == 'refresh' else copy.deepcopy(current)
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
    return {'mode':mode,'new_retrieval':mode=='refresh','depth':depth if mode == 'refresh' else None,'verify_top':verify_top if mode == 'refresh' else None,
            'top':top if mode == 'refresh' else None,'output':str(output),'created_dates':created,
            'periods':[{ 'period':f['period'],'rows':len(f['rows']),'status':f['coverage']['status'],'verified_rows':f['coverage'].get('verified_rows'),
                        'tail':f['coverage'].get('tail'),'verified_passes':f['coverage'].get('verified_passes'),'fetched_at_end':f['fetched_at_end']} for f in data['feeds']]}


def depth_argument(value):
    if value == 'all':
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("depth must be 'all' or a multiple of 100") from None


def main():
    p=argparse.ArgumentParser();p.add_argument('--site-dir',required=True);p.add_argument('--output-dir',required=True)
    p.add_argument('--mode',choices=('refresh','validate-current'),default='refresh');p.add_argument('--max-pages',type=int,default=1000)
    p.add_argument('--depth',type=depth_argument,default='all',help="rows per period: all (default, whole feed) or a multiple of 100 (today's bounded behaviour for that depth)")
    p.add_argument('--verify-top',type=int,default=2000,help='rows per period read twice and published only when both reads agree (multiple of 100, 100..10000)')
    p.add_argument('--top',type=int,default=None,help='alias for --depth N --verify-top N; 0 reads the whole feed under the strict rule')
    p.add_argument('--checkpoint-dir');p.add_argument('--diagnostic');a=p.parse_args()
    try:
        result=stage_site(a.site_dir,a.output_dir,a.mode,a.max_pages,a.checkpoint_dir,top=a.top,depth=a.depth,verify_top=a.verify_top);code=0
    except Exception as exc:
        result={'mode':a.mode,'status':'failed','error':f'{type(exc).__name__}: {str(exc)[:500]}','current_site_preserved':True};code=2
    if a.diagnostic:
        path=pathlib.Path(a.diagnostic);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2));return code
if __name__=='__main__':raise SystemExit(main())
