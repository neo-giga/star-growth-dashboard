"""Rebuild windows/latest.json from a checkout of the data branch, as of its last capture.

The data branch does not commit windows/latest.json (docs/data-layout.md, "Growth of the branch"): it is derived from the day's
snapshot, the snapshots N days before it, the history shards, the history queue, repos.json, meta.json and that day's capture
diagnostic (its completeness), all of which are committed. The page's
publisher runs this on its checkout of the branch and reads windows/latest.json as before; the bytes equal the capture run's.
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from capture.store import DataDir
from capture.windows import build_page


def build(data):
    """Write windows/latest.json for meta.json's last_capture_date and return the page (None when nothing was captured yet)."""
    meta = data.read_meta()
    if not meta.get('last_capture_date'):
        return None
    date = dt.date.fromisoformat(meta['last_capture_date'])
    snapshot = json.loads(data.snapshot_path(date).read_bytes())
    counts = dict(zip(snapshot['node_id'], snapshot['stars']))
    first = dt.date.fromisoformat(meta['first_snapshot_date']) if meta.get('first_snapshot_date') else None
    diagnostic = data.root / 'diagnostics' / date.isoformat()[:4] / f'{date.isoformat()}.json'
    capture = json.loads(diagnostic.read_bytes()).get('completeness') if diagnostic.is_file() else None
    page = build_page(date, snapshot['captured_at'], snapshot['threshold'], data.read_repos()['repos'], counts, data.snapshot_dates(), data.read_snapshot,
                      data.read_history_all(), first, pending=data.read_pending(), capture=capture)
    data.write_windows(page)
    return page


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--data-dir', required=True, help='a checkout of the data branch; windows/latest.json is written into it')
    args = parser.parse_args(argv)
    page = build(DataDir(args.data_dir))
    if page is None:
        print('no capture in this data dir (meta.json has no last_capture_date)', file=sys.stderr)
        return 2
    print(f'windows/latest.json: {page["row_count"]} rows for {page["date"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
