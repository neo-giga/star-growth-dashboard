"""Star gains over 1, 7, 15, 30, 90, 180 and 365 days for every repo above the threshold: the page-ready file.

A window of N days is the N x 24 hours ending at today's capture (Pacific date D, instant T), per repo:
  snapshot: today's count - the count in the snapshot dated D-N                     (exact net change)
            (the cron is in UTC, so that snapshot was captured at T - N days: both ends are captures)
  history:  the same interval from the history buckets: the Pacific day holding T - N days counted pro rata from that instant,
            the later days through the repo's anchor day, + (today's count - anchor count)
            (stars given in the window that still exist; the anchor day is partial, which the anchor term completes)
Both sources cover the same instants, so a column does not jump when it flips (a day-aligned history window was 22.5 h of
the 1-day snapshot's 24 h at 22:30 PDT, 21.5 h in PST: 6-10% lower before the flip).
A window's `source` is `snapshot` when the snapshot dated D-N exists, else `history`; `exact_since` = first snapshot date + N.
A missed capture day is never served by an older snapshot (that would label a longer interval as N days): its rows use
history. A row whose baseline snapshot lacks it (a repo discovered later, or that day missing) falls back to history and sets
its bit in `history_mask` (bit i = window i, in WINDOWS order); a row that neither source can serve has null.
A row whose history is queued (history-pending.json) and not read yet has `history_pending` 1: its nulls are pending history,
not a failure. Only repos that entered without history are queued; a repo whose history record is missing is not pending.
"""
from __future__ import annotations
import datetime as dt
from .history import PACIFIC, WINDOWS, pacific_date, parse_instant, stars_between

PAGE_SCHEMA = 'star-growth-dashboard/windows/1'


def column(days):
    return f'gain_{days}d'


def build_page(date, generated_at, threshold, repos, counts, snapshot_dates, read_snapshot, history, first_snapshot_date, pending=None, capture=None):
    """repos: {node_id: {full_name, language, created_at}}; counts: today's {node_id: stars}; snapshot_dates: every date that has a
    snapshot; read_snapshot(date) -> {node_id: stars}; history: {node_id: folded record}; first_snapshot_date: the ring's start;
    pending: the history queue {node_id: entry}; capture: the day's completeness (scripts/daily_capture.py), carried as is."""
    pending = pending or {}
    rows = sorted((node_id for node_id, stars in counts.items() if stars > threshold), key=lambda i: (-counts[i], repos.get(i, {}).get('full_name') or ''))
    now = parse_instant(generated_at)
    windows, baselines = [], {}
    for index, days in enumerate(WINDOWS):
        target = date - dt.timedelta(days=days)
        baseline = target if target in snapshot_dates else None
        baselines[days] = (baseline, read_snapshot(baseline) if baseline else None)
        windows.append({'days': days, 'column': column(days), 'source': 'snapshot' if baseline else 'history',
                        'exact_since': (first_snapshot_date + dt.timedelta(days=days)).isoformat() if first_snapshot_date else None,
                        'baseline_date': baseline.isoformat() if baseline else None, 'rows_snapshot': 0, 'rows_history': 0, 'rows_null': 0, 'rows_pending': 0})
    columns = {name: [] for name in ('node_id', 'full_name', 'language', 'created_at', 'stars')}
    for days in WINDOWS:
        columns[column(days)] = []
    columns['history_mask'] = []
    columns['history_pending'] = []
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
        waiting = node_id in pending and record is None
        for index, days in enumerate(WINDOWS):
            baseline, base_counts = baselines[days]
            gain = None
            if base_counts is not None and node_id in base_counts:
                gain = stars - base_counts[node_id]
                windows[index]['rows_snapshot'] += 1
            else:
                gain = history_gain(record, now, days, stars)
                if gain is None:
                    windows[index]['rows_null'] += 1
                    windows[index]['rows_pending'] += waiting
                else:
                    mask |= 1 << index
                    windows[index]['rows_history'] += 1
            columns[column(days)].append(gain)
        columns['history_mask'].append(mask)
        columns['history_pending'].append(int(waiting))
    return {'schema': PAGE_SCHEMA, 'generated_at': generated_at, 'date': date.isoformat(), 'threshold': threshold, 'row_count': len(rows),
            'first_snapshot_date': first_snapshot_date.isoformat() if first_snapshot_date else None, 'windows': windows,
            'history': {'pending_rows': sum(columns['history_pending']), 'rows_with_history': sum(1 for i in rows if i in history)}, 'capture': capture,
            'columns': columns}


def history_gain(record, now, days, stars):
    """The history-sourced gain over the N x 24 hours ending at the capture instant `now`, or None when the record cannot serve
    it (no record, its anchor before the window start, or the window starting before the fetched history while older history
    exists). The start day's bucket counts pro rata from the window start (stars spread evenly through that day). Clamped to
    0..stars: history counts only stars still present, so a gain above `stars` is stars given between the anchor and the history
    read counted twice (Gathin23/cleanyourtv on 2026-10-29: 45,328 of 45,327), and an unstar after the anchor cannot make the
    stars given in the window negative."""
    if not record or record.get('start') is None:
        return None
    anchor_at = parse_instant(record['anchor_at'])
    start = now - dt.timedelta(days=days)
    if anchor_at < start:
        return None
    first = pacific_date(start)
    head = stars_between(record, first, first)
    rest = stars_between(record, first + dt.timedelta(days=1), dt.date.fromisoformat(record['through']))
    if head is None or rest is None:
        return None
    day_start = dt.datetime.combine(first, dt.time(), PACIFIC).astimezone(dt.timezone.utc)  # in UTC: a DST day is 23 or 25 hours
    covered = min(dt.datetime.combine(first + dt.timedelta(days=1), dt.time(), PACIFIC).astimezone(dt.timezone.utc), anchor_at)
    share = (covered - start) / (covered - day_start) if covered > day_start else 0
    return max(0, min(stars, round(head * share + rest + stars - record['anchor_stars'])))
