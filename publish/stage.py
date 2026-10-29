"""Stage the site from the data branch's `windows/latest.json`, or refuse.

The gate fails closed (the run goes red, nothing is deployed, the current page stays) when the file is older than MAX_AGE_HOURS,
when `row_count` is under COVERAGE_FLOOR of the search total in that day's diagnostic, or when any gain column is null for more
than NULL_CEILING of the rows. The staged site is the checkout's UI sources (hashed by scripts/build_static_release.py) plus the
page file (the columnar interface without the node IDs) and the agent feed under api/v2. Nothing is committed to git.
"""
from __future__ import annotations
import datetime as dt, json, pathlib, shutil, subprocess, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from capture.history import WINDOWS
from capture.windows import PAGE_SCHEMA, column
from publish.agent_feed import build_agent_feed

PAGE_FILE_SCHEMA = 'star-growth-dashboard/page/1'
MAX_AGE_HOURS = 36
COVERAGE_FLOOR = 0.99
NULL_CEILING = 0.01
GAIN_COLUMNS = tuple(column(days) for days in WINDOWS)
COLUMN_NAMES = ('node_id', 'full_name', 'language', 'created_at', 'stars', *GAIN_COLUMNS, 'history_mask')
PUBLIC_SOURCE_FILES = ('.nojekyll', 'llms.txt', 'docs/agent-feed.md', 'docs/publish-page.yml')


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
    return doc


def gate(doc, diagnostic, now):
    """(problems, measurements) for a validated windows file against that day's capture diagnostic; an empty problems list passes."""
    problems = []
    generated = parse_instant(doc['generated_at'])
    age_hours = (now - generated).total_seconds() / 3600
    measurements = {'generated_at': doc['generated_at'], 'age_hours': round(age_hours, 2), 'max_age_hours': MAX_AGE_HOURS, 'row_count': doc['row_count'],
                    'search_total': None, 'coverage': None, 'coverage_floor': COVERAGE_FLOOR, 'null_share': {}, 'null_ceiling': NULL_CEILING}
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
    for name in GAIN_COLUMNS:
        nulls = sum(1 for value in doc['columns'][name] if value is None)
        share = nulls / doc['row_count'] if doc['row_count'] else 1.0
        measurements['null_share'][name] = round(share, 4)
        if share > NULL_CEILING:
            problems.append(f'{name} is null for {nulls} of {doc["row_count"]} rows ({share:.1%}), over {NULL_CEILING:.0%}')
    return problems, measurements


def page_file(doc):
    """The file the page loads: the windows file without the node IDs (a third of its bytes; the page joins nothing by ID)."""
    columns = {name: doc['columns'][name] for name in COLUMN_NAMES if name != 'node_id'}
    return {'schema': PAGE_FILE_SCHEMA, 'generated_at': doc['generated_at'], 'date': doc['date'], 'threshold': doc['threshold'], 'row_count': doc['row_count'],
            'first_snapshot_date': doc.get('first_snapshot_date'), 'windows': doc['windows'], 'columns': columns}


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


def stage_site(site_dir, data_dir, output_dir, now=None, ignore_gate=False):
    """Stage the site under output_dir (a new directory) from the data branch checkout, or raise GateRefused / ValueError.
    ignore_gate stages whatever the file holds (local previews only; the workflow never passes it): the problems are returned, not raised."""
    site, output = pathlib.Path(site_dir).resolve(), pathlib.Path(output_dir).resolve()
    if output.exists() or output == site or site in output.parents:
        raise ValueError('output must be a new directory outside the checkout')
    ui = read_ui_source(site)
    doc, diagnostic = read_data(data_dir)
    problems, measurements = gate(doc, diagnostic, now or dt.datetime.now(dt.timezone.utc))
    if problems and not ignore_gate:
        raise GateRefused(problems, measurements)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.publish-', dir=output.parent) as temp:
        temp = pathlib.Path(temp); stage = temp / 'site'; stage.mkdir(); copy_public_site(site, stage)
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
            'windows': [{k: w.get(k) for k in ('column', 'source', 'exact_since', 'rows_snapshot', 'rows_history', 'rows_null')} for w in doc['windows']],
            'measurements': measurements, 'gate_ignored': sorted(problems) if ignore_gate else [], 'page_file_bytes': len(encoded), 'artifact_bytes': total, 'snapshot_id': catalog['snapshot_id']}
