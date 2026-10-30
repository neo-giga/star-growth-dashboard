"""The data branch's files (docs/data-layout.md is the reference):

repos.json                      the known set: {threshold, updated_at, repos: {node_id: {full_name, language, created_at, first_seen}}} (no per-day field)
slices.json                     the next discovery's star-range plan
meta.json                       first_snapshot_date, last_total_count, last_capture_date
snapshots/YYYY/YYYY-MM-DD.json  one star count per known repo per Pacific day, kept forever (plain JSON: git delta-compresses it between days)
history/XX.json                 per-day star history by node ID, 256 shards by the SHA-1 of the ID
windows/latest.json             the page-ready file (the interface the page builds on), not committed (.gitignore): scripts/build_windows.py derives it
diagnostics/YYYY/YYYY-MM-DD.json    the day's run diagnostic
Every write is atomic (temporary file + rename), so a killed run leaves whole files only.
"""
from __future__ import annotations
import datetime as dt, hashlib, json, os, pathlib, tempfile

SNAPSHOT_SCHEMA = 'star-growth-dashboard/snapshot/1'
HISTORY_SHARDS = 256


def write_atomic(path, data: bytes):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dumps(doc, compact=True):
    return (json.dumps(doc, separators=(',', ':'), ensure_ascii=False) if compact else json.dumps(doc, indent=1, ensure_ascii=False)).encode()


def shard_of(node_id):
    return hashlib.sha1(node_id.encode()).hexdigest()[:2]


class DataDir:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self._snapshots = {}

    # -- small documents ---------------------------------------------------------------------------------------------
    def _read_json(self, name, default):
        path = self.root / name
        if not path.exists():
            return default
        with open(path, 'rb') as handle:
            return json.load(handle)

    def read_repos(self):
        return self._read_json('repos.json', {'threshold': None, 'updated_at': None, 'repos': {}})

    def write_repos(self, doc):
        write_atomic(self.root / 'repos.json', dumps(doc))

    def read_slices(self):
        return self._read_json('slices.json', None)

    def write_slices(self, plan):
        write_atomic(self.root / 'slices.json', dumps(plan, compact=False))

    def read_meta(self):
        return self._read_json('meta.json', {})

    def write_meta(self, meta):
        write_atomic(self.root / 'meta.json', dumps(meta, compact=False))

    # -- snapshots ---------------------------------------------------------------------------------------------------
    def snapshot_path(self, date):
        return self.root / 'snapshots' / date.isoformat()[:4] / f'{date.isoformat()}.json'

    def snapshot_dates(self):
        root = self.root / 'snapshots'
        if not root.exists():
            return []
        return sorted(dt.date.fromisoformat(p.name[:10]) for p in root.glob('*/*.json'))

    def read_snapshot(self, date):
        """{node_id: stars} of one day (cached), or None when that day has no snapshot."""
        if date not in self._snapshots:
            path = self.snapshot_path(date)
            if not path.exists():
                return None
            with open(path, 'rb') as handle:
                doc = json.load(handle)
            self._snapshots[date] = dict(zip(doc['node_id'], doc['stars']))
        return self._snapshots[date]

    def write_snapshot(self, date, captured_at, counts, threshold):
        ids = sorted(counts)
        doc = {'schema': SNAPSHOT_SCHEMA, 'date': date.isoformat(), 'captured_at': captured_at, 'threshold': threshold, 'rows': len(ids),
               'node_id': ids, 'stars': [counts[i] for i in ids]}
        write_atomic(self.snapshot_path(date), dumps(doc))
        self._snapshots[date] = dict(counts)

    # -- history -----------------------------------------------------------------------------------------------------
    def history_path(self, shard):
        return self.root / 'history' / f'{shard}.json'

    def read_history_all(self):
        out = {}
        root = self.root / 'history'
        if root.exists():
            for path in sorted(root.glob('*.json')):
                with open(path, 'rb') as handle:
                    out.update(json.load(handle))
        return out

    def write_history(self, records):
        """Merge {node_id: record} into the shards (a later record for the same ID replaces the earlier one)."""
        by_shard = {}
        for node_id, record in records.items():
            by_shard.setdefault(shard_of(node_id), {})[node_id] = record
        for shard, fresh in by_shard.items():
            path = self.history_path(shard)
            current = {}
            if path.exists():
                with open(path, 'rb') as handle:
                    current = json.load(handle)
            current.update(fresh)
            write_atomic(path, dumps(dict(sorted(current.items()))))

    # -- windows and diagnostics -------------------------------------------------------------------------------------
    def write_windows(self, page):
        write_atomic(self.root / 'windows' / 'latest.json', dumps(page))
        # Derived from the snapshots and history, and its gains change every day: committed daily it cost ~395 KiB of pack a day
        # (~141 MiB a year, measured 2026-10-29), so the branch ignores it and the publisher rebuilds it (scripts/build_windows.py).
        write_atomic(self.root / '.gitignore', b'/windows/\n')

    def write_diagnostic(self, date, doc):
        write_atomic(self.root / 'diagnostics' / date.isoformat()[:4] / f'{date.isoformat()}.json', dumps(doc, compact=False))
