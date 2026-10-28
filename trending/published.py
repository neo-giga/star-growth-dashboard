"""The previously published dashboard state: the live GitHub Pages site first, the checkout's newest data file as fallback.

Nothing here writes to the live site or the checkout. Generated data is no longer committed to git, so the live site
(index.html, the app it references, the per-window data files the app references, api/v1/catalog.json and the one
snapshot it points to) is the only complete record of what was last published.
"""
from __future__ import annotations
import hashlib
import json
import re
import urllib.request
from pathlib import Path

LIVE_SITE = 'https://neo-giga.github.io/star-growth-dashboard/'
PERIODS = ('day', 'week', 'month')
MAX_BYTES = 64 * 1024 * 1024
USER_AGENT = 'star-growth-dashboard (+https://github.com/neo-giga/star-growth-dashboard)'
PATH = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*')
DATA_FILE = re.compile(r'data(?:-(day|week|month))?(?:\.[0-9a-f]+)?\.json')


def fetch_bytes(url):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('published file exceeds 64 MiB bound')
    return data


def safe_path(relative):
    if not PATH.fullmatch(relative) or '..' in relative.split('/'):
        raise ValueError('unsafe published path')
    return relative


class LiveSite:
    """Reads files of the deployed site over HTTPS; `requests` counts fetches."""
    def __init__(self, base_url=LIVE_SITE, fetch=fetch_bytes):
        if not base_url.startswith('http'):
            raise ValueError('published site must be an http(s) URL')
        self.base = base_url.rstrip('/') + '/'
        self.fetch = fetch
        self.requests = 0

    def read(self, relative):
        self.requests += 1
        return self.fetch(self.base + safe_path(relative))

    def __str__(self):
        return self.base


class LocalSite:
    """Reads files of a site tree on disk (a checkout or a staged artifact); never follows symlinks."""
    def __init__(self, root):
        self.root = Path(root).resolve()

    def read(self, relative):
        path = self.root / safe_path(relative)
        if path.is_symlink() or not path.is_file() or self.root not in path.resolve().parents:
            raise ValueError('missing/nonregular published file: ' + relative)
        return path.read_bytes()

    def __str__(self):
        return str(self.root)


def validate_dashboard(data):
    if not isinstance(data, dict) or not isinstance(data.get('feeds'), list):
        raise ValueError('invalid dashboard')
    periods = set()
    for feed in data['feeds']:
        period = feed.get('period')
        if feed.get('provider') != 'gittrend' or period not in PERIODS or period in periods:
            raise ValueError('foreign/duplicate feed')
        periods.add(period)
        if not isinstance(feed.get('rows'), list):
            raise ValueError('invalid rows')
        ids = set()
        for row in feed['rows']:
            if type(row.get('repo_id')) is not int or row['repo_id'] in ids or not isinstance(row.get('full_name'), str):
                raise ValueError('invalid/duplicate provider identity')
            ids.add(row['repo_id'])
    if periods != set(PERIODS):
        raise ValueError('all three separate periods required')


def data_references(app):
    """{period: file name} for an app with one data file per window, or {'all': name} for the older single-file app."""
    names = {m[2]: m[1] for m in re.finditer(r"'(data-(day|week|month)\.[0-9a-f]+\.json)'", app)}
    if len(names) == 3:
        return names
    single = re.search(r"fetch\('(data(?:\.[0-9a-f]+)?\.json)'", app)
    if not single:
        raise ValueError('app references no data file')
    return {'all': single[1]}


def merge_window_files(files):
    """One dashboard dict out of {period: parsed per-window file}; the files must agree on generation and snapshot."""
    head = None
    feeds = []
    for period in PERIODS:
        payload = files[period]
        if not isinstance(payload, dict) or not isinstance(payload.get('feeds'), list) or len(payload['feeds']) != 1:
            raise ValueError(f'{period} data file must hold exactly one feed')
        if payload['feeds'][0].get('period') != period:
            raise ValueError(f'{period} data file holds another period')
        meta = {k: v for k, v in payload.items() if k != 'feeds'}
        if head is None:
            head = meta
        elif meta != head:
            raise ValueError('window data files come from different publications')
        feeds.append(payload['feeds'][0])
    return dict(head, feeds=feeds)


def read_site_data(site):
    """The dashboard data a site publishes, read the way a browser would (index.html -> app -> data files), plus the file names."""
    index = site.read('index.html').decode('utf-8')
    app_name = re.search(r'<script[^>]+src="([^"]+)"', index)
    if not app_name:
        raise ValueError('missing UI assets')
    app = site.read(app_name[1]).decode('utf-8')
    references = data_references(app)
    if 'all' in references:
        data = json.loads(site.read(references['all']))
    else:
        data = merge_window_files({period: json.loads(site.read(name)) for period, name in references.items()})
    validate_dashboard(data)
    return data, {'index.html': 'index.html', 'app': app_name[1], 'data': references}


def checkout_data_files(root):
    """Dashboard data files in a checkout root, newest publication first: [(generated_at, names, data)]. Per-window files are grouped
    by their shared generation metadata; incomplete groups and unreadable files are skipped."""
    root = Path(root)
    singles, groups = [], {}
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file() or not DATA_FILE.fullmatch(path.name):
            continue
        try:
            payload = json.loads(path.read_bytes())
        except ValueError:
            continue
        period = DATA_FILE.fullmatch(path.name)[1]
        if period is None:
            try:
                validate_dashboard(payload)
            except ValueError:
                continue
            singles.append((payload.get('generated_at') or '', [path.name], payload))
        elif isinstance(payload, dict) and isinstance(payload.get('feeds'), list):
            key = json.dumps({k: v for k, v in payload.items() if k != 'feeds'}, sort_keys=True)
            groups.setdefault(key, {})[period] = (path.name, payload)
    for members in groups.values():
        if set(members) != set(PERIODS):
            continue
        try:
            data = merge_window_files({p: members[p][1] for p in PERIODS})
            validate_dashboard(data)
        except ValueError:
            continue
        singles.append((data.get('generated_at') or '', [members[p][0] for p in PERIODS], data))
    singles.sort(key=lambda item: item[0], reverse=True)
    return singles


def seed_created_dates(candidates):
    """{(provider id, casefolded name): (created_at, github id)} from published data, newest first wins."""
    seeds = {}
    for _, _, data in candidates:
        for feed in data['feeds']:
            for row in feed['rows']:
                if row.get('created_at') is None:
                    continue
                seeds.setdefault((row['repo_id'], row['full_name'].casefold()), (row['created_at'], row.get('created_at_github_repo_id')))
    return seeds


def read_snapshot(site, snapshot_id, want='all'):
    """The one published api/v1 snapshot: {'id', 'catalog', 'schema', 'files': {relative path under the snapshot: bytes}} verified
    against the manifest's byte lengths and SHA-256 digests. want='records' reads only each period's all.jsonl."""
    catalog_bytes = site.read('api/v1/catalog.json')
    catalog = json.loads(catalog_bytes)
    if catalog.get('snapshot_id') != snapshot_id:
        raise ValueError(f'published catalog points at snapshot {catalog.get("snapshot_id")}, data at {snapshot_id}')
    if not re.fullmatch(r'[0-9a-f]{64}', snapshot_id):
        raise ValueError('invalid snapshot id')
    prefix = f'api/v1/snapshots/{snapshot_id}/'
    manifest_bytes = site.read(prefix + 'manifest.json')
    manifest = json.loads(manifest_bytes)
    if manifest.get('snapshot_id') != snapshot_id:
        raise ValueError('manifest snapshot id mismatch')
    files = {}
    for entry in manifest['files']:
        name = safe_path(entry['path'])
        if want == 'records' and not name.endswith('/all.jsonl'):
            continue
        data = site.read(prefix + name)
        if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('published snapshot file does not match its manifest: ' + name)
        files[name] = data
    files['manifest.json'] = manifest_bytes
    return {'id': snapshot_id, 'catalog': catalog, 'catalog_bytes': catalog_bytes, 'files': files,
            'schema': site.read('api/v1/schema.json') if want == 'all' else None}


def snapshot_records(snapshot):
    """{period: {record_id: record}} from a snapshot's all.jsonl files."""
    records = {}
    for period in PERIODS:
        lines = snapshot['files'].get(f'{period}/all.jsonl')
        if lines is None:
            continue
        records[period] = {}
        for line in lines.splitlines():
            if line.strip():
                record = json.loads(line)
                records[period][record['record_id']] = record
    return records


def hydrate_metadata(data, records, metadata_fields):
    """Attach the published snapshot's cached GitHub metadata to rows of the same period, provider ID and name; rows without a
    matching record keep metadata None. Returns the number of rows hydrated."""
    hydrated = 0
    for feed in data.get('feeds', []):
        for row in feed.get('rows', []):
            record = records.get(feed['period'], {}).get(f"gittrend:{row['repo_id']}")
            if not record or record['identity'].get('full_name') != row['full_name']:
                continue
            p = record.get('provenance') or {}
            row['metadata'] = {k: record['metadata'].get(k) for k in metadata_fields}
            row['metadata'].update(source=p.get('metadata_source'), observed_at=p.get('metadata_observed_at'),
                                   contributor_measured_at=p.get('contributor_measured_at'),
                                   unknown_flags=p.get('unknown_flags', []), github_repo_id=record['identity'].get('github_metadata_repo_id'))
            hydrated += 1
    return hydrated


def write_snapshot(snapshot, stage):
    """Place a verified snapshot (as read_snapshot returns it with want='all') under stage/api/v1 and return its catalog."""
    root = Path(stage) / 'api/v1'
    target = root / 'snapshots' / snapshot['id']
    for name, data in snapshot['files'].items():
        path = target / safe_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / 'schema.json').write_bytes(snapshot['schema'])
    (root / 'catalog.json').write_bytes(snapshot['catalog_bytes'])
    return snapshot['catalog']
