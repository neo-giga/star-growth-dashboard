"""Stage the site from the data branch's `windows/latest.json`, or refuse.

The gate fails closed (the run goes red, nothing is deployed, the current page stays) when the file is older than MAX_AGE_HOURS,
when `row_count` is under COVERAGE_FLOOR of the search total in that day's diagnostic, when that diagnostic says a count batch
stayed unread (known or new repositories), or when any gain column is null for more than NULL_CEILING of the rows whose history
is not pending, unless a missed capture explains it (missed_capture_columns). Pending history is the queued entrants whose
history is not read yet (`history_pending` 1, capture/windows.py); a repo whose history record went missing is never pending.
The staged site is the checkout's UI sources (hashed by scripts/build_static_release.py) plus the
page file (the columnar interface without the node IDs) and the agent feed under api/v2, plus the previous publication's hashed
script, stylesheet and data files (one generation) so a page loaded before the deploy still finds them. Nothing is committed to git.
"""
from __future__ import annotations
import datetime as dt, json, pathlib, re, shutil, subprocess, sys, tempfile, urllib.parse, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from capture.history import WINDOWS
from capture.windows import PAGE_SCHEMA, column
from publish.agent_feed import build_agent_feed

PAGE_FILE_SCHEMA = 'star-growth-dashboard/page/1'
MAX_AGE_HOURS = 36
COVERAGE_FLOOR = 0.99
NULL_CEILING = 0.01
GAIN_COLUMNS = tuple(column(days) for days in WINDOWS)
COLUMN_NAMES = ('node_id', 'full_name', 'language', 'created_at', 'stars', *GAIN_COLUMNS, 'history_mask')  # history_pending is optional (pending_column)
PUBLIC_SOURCE_FILES = ('.nojekyll', 'llms.txt', 'docs/agent-feed.md', 'docs/publish-page.yml')
HASHED_ASSET = re.compile(r'(?:app|style|data(?:-[a-z]+)?)\.[0-9a-f]{16}\.(?:js|css|json)')
PAGE_ASSET = re.compile(r'data\.[0-9a-f]{16}\.json')
MAX_ASSET_BYTES = 64 * 1024 * 1024


class GateRefused(Exception):
    """The windows file is not publishable; `problems` says why and `measurements` what was measured."""
    def __init__(self, problems, measurements):
        super().__init__('; '.join(problems))
        self.problems, self.measurements = problems, measurements


def parse_instant(value):
    if not isinstance(value, str):
        raise ValueError('generated_at must be an ISO instant')
    instant = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('generated_at must carry a timezone')
    return instant


def validate_windows(doc):
    """The columnar windows file in the documented interface shape (docs/data-layout.md), or ValueError."""
    if not isinstance(doc, dict) or doc.get('schema') != PAGE_SCHEMA:
        raise ValueError(f'windows file schema must be {PAGE_SCHEMA}')
    parse_instant(doc.get('generated_at'))
    if not isinstance(doc.get('date'), str):
        raise ValueError('windows file lacks its date')
    dt.date.fromisoformat(doc['date'])
    count = doc.get('row_count')
    if type(count) is not int or count < 0:
        raise ValueError('row_count must be a nonnegative integer')
    if type(doc.get('threshold')) is not int:
        raise ValueError('threshold must be an integer')
    if 'history_pending' in (doc.get('columns') or {}):
        pending = doc['columns']['history_pending']
        if not isinstance(pending, list) or len(pending) != count or any(value not in (0, 1) or type(value) is not int for value in pending):
            raise ValueError(f'column history_pending must hold row_count ({count}) values of 0 or 1')
        if any(flag and mask for flag, mask in zip(pending, doc['columns'].get('history_mask') or [])):
            raise ValueError('a row with pending history cannot carry a history-sourced gain')
    columns = doc.get('columns')
    if not isinstance(columns, dict):
        raise ValueError('columns must be an object')
    for name in COLUMN_NAMES:
        if not isinstance(columns.get(name), list) or len(columns[name]) != count:
            raise ValueError(f'column {name} must hold row_count ({count}) values')
    windows = doc.get('windows')
    if not isinstance(windows, list) or [w.get('column') if isinstance(w, dict) else None for w in windows] != list(GAIN_COLUMNS):
        raise ValueError('windows must describe the seven gain columns in order')
    for window in windows:
        if window.get('source') not in ('history', 'snapshot'):
            raise ValueError(f'{window.get("column")}: source must be history or snapshot')
    for name in GAIN_COLUMNS:
        if any(value is not None and type(value) is not int for value in columns[name]):
            raise ValueError(f'column {name} must hold integers or null')
    if any(type(value) is not int for value in columns['stars']) or any(type(value) is not int for value in columns['history_mask']):
        raise ValueError('stars and history_mask must hold integers')
    if any(not isinstance(value, str) or not value for value in columns['node_id']):
        raise ValueError('every node_id must be a nonempty string')
    if len(set(columns['node_id'])) != count:
        raise ValueError(f'node_id repeats: {count - len(set(columns["node_id"]))} rows duplicate another')
    below = sum(1 for value in columns['stars'] if value <= doc['threshold'])
    if below:
        raise ValueError(f'{below} rows have stars at or below the threshold {doc["threshold"]}')
    return doc


def pending_column(doc):
    """history_pending per row (0 for a file written before the column existed: no row is exempt)."""
    return doc['columns'].get('history_pending') or [0] * doc['row_count']


def count_health(diagnostic):
    """(known unread, new unread) from the capture diagnostic: its `counts` block, or the failed queries of an older diagnostic."""
    counts = diagnostic.get('counts') if isinstance(diagnostic, dict) else None
    if isinstance(counts, dict):
        return counts.get('known_unread') or 0, counts.get('entrants_unread') or 0
    snapshot = diagnostic.get('snapshot') if isinstance(diagnostic, dict) else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    entrants = ((diagnostic.get('new_entrants') or {}).get('snapshot') or {}) if isinstance(diagnostic, dict) else {}
    return snapshot.get('failed_queries') or 0, entrants.get('failed_queries') or 0


def gate(doc, diagnostic, now):
    """(problems, measurements) for a validated windows file against that day's capture diagnostic; an empty problems list passes."""
    problems = []
    generated = parse_instant(doc['generated_at'])
    age_hours = (now - generated).total_seconds() / 3600
    measurements = {'generated_at': doc['generated_at'], 'age_hours': round(age_hours, 2), 'max_age_hours': MAX_AGE_HOURS, 'row_count': doc['row_count'],
                    'search_total': None, 'coverage': None, 'coverage_floor': COVERAGE_FLOOR, 'null_share': {}, 'null_share_not_pending': {}, 'null_ceiling': NULL_CEILING,
                    'null_exempt': missed_capture_columns(doc), 'pending_rows': 0, 'unread': None, 'capture': doc.get('capture')}
    if age_hours > MAX_AGE_HOURS:
        problems.append(f'windows file is {age_hours:.1f} h old (generated {doc["generated_at"]}), over {MAX_AGE_HOURS} h')
    total = None
    if isinstance(diagnostic, dict) and diagnostic.get('date') == doc['date'] and isinstance(diagnostic.get('coverage'), dict):
        total = diagnostic['coverage'].get('total_count')
    if type(total) is not int or total <= 0:
        problems.append(f'no search total in the capture diagnostic for {doc["date"]}')
    else:
        measurements['search_total'] = total
        measurements['coverage'] = round(doc['row_count'] / total, 4)
        if doc['row_count'] < COVERAGE_FLOOR * total:
            problems.append(f'row_count {doc["row_count"]} is under {COVERAGE_FLOOR:.0%} of the search total {total}')
        known_unread, new_unread = count_health(diagnostic)
        measurements['unread'] = {'known': known_unread, 'new': new_unread}
        if known_unread or new_unread:
            problems.append(f'the capture left {known_unread} known and {new_unread} new repositories unread (failed count batches)')
    pending = pending_column(doc)
    measurements['pending_rows'] = sum(pending)
    settled = doc['row_count'] - sum(pending)
    for name in GAIN_COLUMNS:
        values = doc['columns'][name]
        nulls = sum(1 for value in values if value is None)
        measurements['null_share'][name] = round(nulls / doc['row_count'], 4) if doc['row_count'] else 1.0
        unexplained = sum(1 for value, flag in zip(values, pending) if value is None and not flag)
        share = unexplained / settled if settled else (0.0 if doc['row_count'] else 1.0)
        measurements['null_share_not_pending'][name] = round(share, 4)
        if share > NULL_CEILING and name not in measurements['null_exempt']:
            excluded = f'; {doc["row_count"] - settled} rows with pending history not counted' if settled != doc['row_count'] else ''
            problems.append(f'{name} is null for {unexplained} of {settled} rows ({share:.1%}), over {NULL_CEILING:.0%}{excluded}')
    return problems, measurements


def missed_capture_columns(doc):
    """The gain columns a missed capture N days ago leaves without a baseline today: on or after the window's exact_since (so the
    snapshot ring is deep enough) yet no baseline was read (baseline_date null). PR A then serves the column from history, which
    rarely reaches that far back, so it is mostly null for one day; the null rule exempts it rather than block a publish on each
    of up to 7 later days. Before exact_since a null-heavy column is the missing backfill, and the rule holds."""
    date = dt.date.fromisoformat(doc['date'])
    return [w['column'] for w in doc['windows'] if w.get('baseline_date') is None and isinstance(w.get('exact_since'), str) and date >= dt.date.fromisoformat(w['exact_since'])]


def page_file(doc):
    """The file the page loads: the windows file without the node IDs (a third of its bytes; the page joins nothing by ID)."""
    columns = {name: doc['columns'][name] for name in COLUMN_NAMES if name != 'node_id'}
    columns['history_pending'] = pending_column(doc)
    return {'schema': PAGE_FILE_SCHEMA, 'generated_at': doc['generated_at'], 'date': doc['date'], 'threshold': doc['threshold'], 'min_stars': doc['threshold'] + 1,
            'row_count': doc['row_count'], 'first_snapshot_date': doc.get('first_snapshot_date'), 'windows': doc['windows'],
            'history': {'pending_rows': sum(columns['history_pending']), **{k: v for k, v in (doc.get('history') or {}).items() if k != 'pending_rows'}},
            'capture': doc.get('capture'), 'columns': columns}


def read_data(data_dir):
    """(windows doc, diagnostic or None) from a checkout of the data branch."""
    root = pathlib.Path(data_dir)
    with open(root / 'windows' / 'latest.json', 'rb') as handle:
        doc = validate_windows(json.load(handle))
    path = root / 'diagnostics' / doc['date'][:4] / f'{doc["date"]}.json'
    diagnostic = None
    if path.is_file():
        with open(path, 'rb') as handle:
            diagnostic = json.load(handle)
    return doc, diagnostic


def read_ui_source(site):
    """The UI sources of the checkout: index.html must reference app.js and style.css plainly, app.js the data file."""
    site = pathlib.Path(site)
    ui = {name: (site / name).read_text(encoding='utf-8') for name in ('index.html', 'app.js', 'style.css')}
    if 'src="app.js"' not in ui['index.html'] or 'href="style.css"' not in ui['index.html']:
        raise ValueError('index.html must reference app.js and style.css')
    if "'data.json'" not in ui['app.js']:
        raise ValueError('app.js must reference data.json')
    return ui


def copy_public_site(site, stage):
    """The artifact's hand-written files; nothing else in the checkout is published."""
    for name in PUBLIC_SOURCE_FILES:
        path = pathlib.Path(site) / name
        if path.is_symlink():
            raise ValueError('symlinks are not publishable')
        if path.is_file():
            (stage / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, stage / name)


def read_previous(source, name):
    """The bytes of `name` in the previous publication: the live site (an https URL) or a staged site directory."""
    if str(source).startswith('https://'):
        request = urllib.request.Request(urllib.parse.urljoin(str(source), name), headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_ASSET_BYTES + 1)
        if len(data) > MAX_ASSET_BYTES:
            raise ValueError(f'{name} is over {MAX_ASSET_BYTES} bytes')
        return data
    return (pathlib.Path(source) / name).read_bytes()


def previous_generation(source):
    """{name: bytes} of the hashed script, stylesheet and data files the previous publication's index.html serves, so a cached
    index.html or an already-loaded script still finds them during the deploy. Only that one generation is carried: the next deploy
    carries this one's, never older ones."""
    html = read_previous(source, 'index.html').decode('utf-8')
    carried = {name: read_previous(source, name) for name in re.findall(r'(?:src|href)="([^"]+)"', html) if HASHED_ASSET.fullmatch(name)}
    for script in [name for name in carried if name.endswith('.js')]:
        for name in re.findall(rf"'({HASHED_ASSET.pattern})'", carried[script].decode('utf-8')):
            if name.endswith('.json'):
                carried[name] = read_previous(source, name)
    return carried, gittrend_era(html, carried)


def gittrend_era(html, carried):
    """Why a previous generation is GitTrend's and so not carried (GitTrend is never served again, Neo 2026-10-29; a tab open across
    that one deploy may 404), or None for a v3 publication."""
    if not any(PAGE_ASSET.fullmatch(name) for name in carried):
        return 'no v3 page file (data.<hash>.json)'
    named = [name for name, data in [('index.html', html.encode('utf-8')), *carried.items()] if b'gittrend' in data.lower()]
    return f"'gittrend' in {', '.join(named)}" if named else None


def stage_site(site_dir, data_dir, output_dir, now=None, ignore_gate=False, previous=None):
    """Stage the site under output_dir (a new directory) from the data branch checkout, or raise GateRefused / ValueError.
    ignore_gate stages whatever the file holds (local previews only; the workflow never passes it): the problems are returned, not raised.
    previous: the previous publication (the live site's https URL or a staged site directory) whose hashed assets are carried; when it cannot be read
    or is GitTrend-era nothing is carried and the result says why (the first deploy has none; a missing courtesy copy never blocks a deploy)."""
    site, output = pathlib.Path(site_dir).resolve(), pathlib.Path(output_dir).resolve()
    if output.exists() or output == site or site in output.parents:
        raise ValueError('output must be a new directory outside the checkout')
    ui = read_ui_source(site)
    doc, diagnostic = read_data(data_dir)
    problems, measurements = gate(doc, diagnostic, now or dt.datetime.now(dt.timezone.utc))
    if problems and not ignore_gate:
        raise GateRefused(problems, measurements)
    carried, previous_note = {}, None
    if previous is not None:
        try:
            carried, era = previous_generation(previous)
            if era:
                carried, previous_note = {}, f'previous generation at {previous} is GitTrend-era ({era}); none carried'
        except Exception as exc:  # noqa: BLE001 - reported in the result, never hidden
            previous_note = f'previous generation unreadable from {previous}: {type(exc).__name__}: {str(exc)[:200]}; none carried'
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.publish-', dir=output.parent) as temp:
        temp = pathlib.Path(temp); stage = temp / 'site'; stage.mkdir(); copy_public_site(site, stage)
        for name, data in carried.items():
            (stage / name).write_bytes(data)
        catalog = build_agent_feed(doc, stage)
        source = temp / 'ui'; source.mkdir()
        for name, text in ui.items():
            (source / name).write_text(text, encoding='utf-8')
        payload = page_file(doc)
        payload['agent_catalog_url'] = catalog['catalog_url']
        payload['agent_snapshot_id'] = catalog['snapshot_id']
        encoded = (json.dumps(payload, separators=(',', ':'), ensure_ascii=False) + '\n').encode('utf-8')
        (source / 'data.json').write_bytes(encoded)
        subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'build_static_release.py'), '--source', str(source), '--output', str(stage)],
                       check=True, capture_output=True, text=True)
        total = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
        shutil.move(str(stage), str(output))
    return {'output': str(output), 'date': doc['date'], 'generated_at': doc['generated_at'], 'row_count': doc['row_count'], 'threshold': doc['threshold'],
            'windows': [{k: w.get(k) for k in ('column', 'source', 'exact_since', 'rows_snapshot', 'rows_history', 'rows_null', 'rows_pending')} for w in doc['windows']],
            'measurements': measurements, 'gate_ignored': sorted(problems) if ignore_gate else [], 'page_file_bytes': len(encoded), 'artifact_bytes': total, 'snapshot_id': catalog['snapshot_id'],
            'previous_generation': sorted(carried), 'previous_generation_note': previous_note}
