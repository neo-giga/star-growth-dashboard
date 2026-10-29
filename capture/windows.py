"""Star gains over 1, 7, 15, 30, 90, 180 and 365 days for every repo above the threshold: the page-ready file.

A window of N days ending at today's capture (Pacific date D) is, per repo:
  snapshot: today's count - the count in the newest snapshot dated <= D-N          (exact net change)
  history:  the history days from D-N+1 through the repo's anchor day + (today's count - anchor count)
            (stars given in those days that still exist; the anchor day is partial, which the anchor term completes)
A window's `source` is `snapshot` once the first snapshot is at least N days old (`exact_since` = first snapshot date + N),
else `history`. A row whose baseline snapshot lacks it (a repo discovered later) falls back to history and sets its bit in
`history_mask` (bit i = window i, in WINDOWS order); a row that neither source can serve has null.
"""
from __future__ import annotations
import datetime as dt
from .history import WINDOWS, stars_between

PAGE_SCHEMA = 'star-growth-dashboard/windows/1'


def column(days):
    return f'gain_{days}d'


def build_page(date, generated_at, threshold, repos, counts, snapshot_dates, read_snapshot, history, first_snapshot_date):
    """repos: {node_id: {full_name, language, created_at}}; counts: today's {node_id: stars}; snapshot_dates: every date that has a
    snapshot; read_snapshot(date) -> {node_id: stars}; history: {node_id: folded record}; first_snapshot_date: the ring's start."""
    rows = sorted((node_id for node_id, stars in counts.items() if stars > threshold), key=lambda i: (-counts[i], repos.get(i, {}).get('full_name') or ''))
    windows, baselines = [], {}
    for index, days in enumerate(WINDOWS):
        target = date - dt.timedelta(days=days)
        older = [d for d in snapshot_dates if d <= target]
        baseline = max(older) if older else None
        baselines[days] = (baseline, read_snapshot(baseline) if baseline else None)
        windows.append({'days': days, 'column': column(days), 'source': 'snapshot' if baseline else 'history',
                        'exact_since': (first_snapshot_date + dt.timedelta(days=days)).isoformat() if first_snapshot_date else None,
                        'baseline_date': baseline.isoformat() if baseline else None, 'rows_snapshot': 0, 'rows_history': 0, 'rows_null': 0})
    columns = {name: [] for name in ('node_id', 'full_name', 'language', 'created_at', 'stars')}
    for days in WINDOWS:
        columns[column(days)] = []
    columns['history_mask'] = []
    for node_id in rows:
        meta = repos.get(node_id, {})
        stars = counts[node_id]
        columns['node_id'].append(node_id)
        columns['full_name'].append(meta.get('full_name'))
        columns['language'].append(meta.get('language'))
        columns['created_at'].append(meta.get('created_at'))
        columns['stars'].append(stars)
        mask = 0
        record = history.get(node_id)
        for index, days in enumerate(WINDOWS):
            baseline, base_counts = baselines[days]
            gain = None
            if base_counts is not None and node_id in base_counts:
                gain = stars - base_counts[node_id]
                windows[index]['rows_snapshot'] += 1
            else:
                gain = history_gain(record, date, days, stars)
                if gain is None:
                    windows[index]['rows_null'] += 1
                else:
                    mask |= 1 << index
                    windows[index]['rows_history'] += 1
            columns[column(days)].append(gain)
        columns['history_mask'].append(mask)
    return {'schema': PAGE_SCHEMA, 'generated_at': generated_at, 'date': date.isoformat(), 'threshold': threshold, 'row_count': len(rows),
            'first_snapshot_date': first_snapshot_date.isoformat() if first_snapshot_date else None, 'windows': windows, 'columns': columns}


def history_gain(record, date, days, stars):
    """The history-sourced gain, or None when the record cannot serve the window (no record, the anchor outside the window, or the
    window starting before the fetched history while older history exists)."""
    if not record or record.get('start') is None:
        return None
    through = dt.date.fromisoformat(record['through'])
    first = date - dt.timedelta(days=days - 1)
    if not first <= through <= date:
        return None
    summed = stars_between(record, first, through)
    if summed is None:
        return None
    return summed + (stars - record['anchor_stars'])
