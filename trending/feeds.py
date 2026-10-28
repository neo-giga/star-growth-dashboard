"""Provider-labelled GitTrend adapter. Never writes the collector's SQLite tables.

GitTrend's starsToday is the requested period's provider-reported gain; its
weighted trendingScore is deliberately ignored. The API supplies no snapshot
as-of date. Retrieval time is NOT a replacement for that missing timestamp.
"""
from __future__ import annotations
import datetime as dt
import json
import http.client
import math
import re
import sqlite3
import time
import tempfile
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = 'https://gittrend.io/api/trending'
PERIODS = {'1d': 'day', '7d': 'week', '30d': 'month'}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def number(value, *, nullable=False, nonnegative=False):
    if value is None or value == '' or (isinstance(value, str) and value.lower() in ('unknown', 'null')):
        if nullable:
            return None
        raise ValueError('missing required numeric value')
    if isinstance(value, bool):
        raise ValueError('boolean is not a star count')
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r'[+-]?\d+', value):
        result = int(value)
    else:
        raise ValueError('invalid integer star count or gain')
    if abs(result) > 2**63 - 1:
        raise ValueError('numeric value exceeds supported signed 64-bit range')
    if nonnegative and result < 0:
        raise ValueError('negative total star count')
    return result


class FeedClient:
    def __init__(self, fetch=None, sleeper=time.sleep, max_attempts=3, cache_dir=None, resume=False):
        if max_attempts < 1 or max_attempts > 8:
            raise ValueError('max_attempts must be 1..8')
        self.fetch = fetch or self._fetch
        self.sleeper = sleeper
        self.max_attempts = max_attempts
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.resume = resume
        self.last_fetched_at = None
        self.cached_pages = 0
        self.requests = 0
        self.retries = 0

    @staticmethod
    def _fetch(url):
        request = urllib.request.Request(url, headers={'User-Agent': 'star-growth-dashboard (+https://github.com/neo-giga/star-growth-dashboard)',
                                                       'Accept': 'application/json', 'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('provider response exceeds 20 MiB bound')
        return json.loads(data)

    def page(self, period, page, limit):
        if period not in PERIODS.values() or page < 1 or not 1 <= limit <= 100:
            raise ValueError('invalid period/page/limit')
        url = BASE_URL + '?' + urllib.parse.urlencode({'time': period, 'page': page, 'limit': limit})
        cache_path = self.cache_dir / f'{period}-{limit}-{page:06d}.json' if self.cache_dir else None
        if cache_path and cache_path.exists():
            if not self.resume:
                raise ValueError('checkpoint already exists; choose a new directory or explicitly --resume')
            envelope = json.loads(cache_path.read_text())
            if not isinstance(envelope, dict) or any(envelope.get(k) != v for k, v in {'format': 1, 'provider': 'gittrend', 'url': url}.items()):
                raise ValueError('checkpoint provider/request identity mismatch')
            if not isinstance(envelope.get('fetched_at'), str) or not isinstance(envelope.get('payload'), dict):
                raise ValueError('malformed checkpoint timestamp or payload')
            stamp = dt.datetime.fromisoformat(envelope['fetched_at'])
            if stamp.tzinfo is None:
                raise ValueError('checkpoint fetch time lacks timezone')
            self.last_fetched_at = stamp.isoformat()
            self.cached_pages += 1
            return envelope['payload']
        for attempt in range(self.max_attempts):
            self.requests += 1
            try:
                result = self.fetch(url)
                if not isinstance(result, dict):
                    raise ValueError('provider response is not an object')
                self.last_fetched_at = utcnow()
                if cache_path:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    envelope = {'format': 1, 'provider': 'gittrend', 'url': url,
                                'fetched_at': self.last_fetched_at, 'payload': result}
                    with tempfile.NamedTemporaryFile(mode='w', dir=cache_path.parent, delete=False) as fh:
                        json.dump(envelope, fh)
                        tmp_path = fh.name
                    os.replace(tmp_path, cache_path)
                return result
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 == self.max_attempts:
                    raise
                delay = 2 ** attempt
                retry_after = exc.headers.get('Retry-After') if exc.headers else None
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        raise ValueError('non-numeric Retry-After; retry later') from exc
                    if delay > 60:
                        raise ValueError('provider requests a delay above 60 seconds; retry later') from exc
                self.retries += 1
                self.sleeper(delay)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                if attempt + 1 == self.max_attempts:
                    raise
                self.retries += 1
                self.sleeper(2 ** attempt)


def normalize(row):
    if not isinstance(row, dict):
        raise ValueError('repository row is not an object')
    rid = number(row.get('id'), nonnegative=True)
    name = row.get('fullName')
    if rid == 0 or not isinstance(name, str) or not re.fullmatch(r'[^\s/]+/[^\s/]+', name):
        raise ValueError('invalid provider ID or repository name')
    return {'repo_id': rid, 'full_name': name, 'stars': number(row.get('stars'), nonnegative=True),
            'gain': number(row.get('starsToday'), nullable=True), 'repo_created_at': row.get('createdAt'),
            'language': row.get('language'), 'description': row.get('description'),
            'url': 'https://github.com/' + name}


def collect_feed(client, period, limit=100, max_pages=1000, first_page=1, keep_first=False):
    """first_page > 1 reads a tail continuing an earlier read; keep_first keeps a duplicate ID's best-ranked row."""
    if period not in PERIODS.values() or not 1 <= limit <= 100 or max_pages < 1 or first_page < 1:
        raise ValueError('invalid period/limit/max_pages')
    started = utcnow()
    rows, pages, warnings = {}, [], []
    duplicates = 0
    first_total = None
    reached_end = False
    failure = None
    for page in range(first_page, first_page + max_pages):
        requested_at = utcnow()
        try:
            payload = client.page(period, page, limit)
            requested_at = getattr(client, 'last_fetched_at', None) or requested_at
            items, pg = payload.get('data'), payload.get('pagination')
            if not isinstance(items, list) or not isinstance(pg, dict):
                raise ValueError('missing data or pagination')
            total = number(pg.get('total'), nonnegative=True)
            actual_page = number(pg.get('page'), nonnegative=True)
            actual_limit = number(pg.get('limit'), nonnegative=True)
            total_pages = number(pg.get('totalPages'), nonnegative=True)
            has_more = pg.get('hasMore')
            if actual_page != page or actual_limit != limit or type(has_more) is not bool:
                raise ValueError('provider returned unexpected page/limit/hasMore')
            if total_pages != math.ceil(total / limit) or has_more != (page < total_pages):
                raise ValueError('inconsistent pagination metadata')
            expected = min(limit, max(0, total - (page - 1) * limit))
            normalized = [normalize(item) for item in items]
            if len(items) != expected:
                warnings.append(f'page {page} returned {len(items)}, expected {expected}')
            if first_total is None:
                first_total = total
            elif total != first_total:
                warnings.append(f'provider total changed from {first_total} to {total} at page {page}')
            for row in normalized:
                row['observation'] = {'provider': 'gittrend', 'period': period, 'page': page, 'fetched_at': requested_at,
                                      'source_url': BASE_URL + '?' + urllib.parse.urlencode({'time': period, 'page': page, 'limit': limit}),
                                      'source_as_of': None}
                if row['repo_id'] in rows:
                    duplicates += 1
                    if keep_first:
                        continue
                rows[row['repo_id']] = row
            pages.append({'page': page, 'total': total, 'returned': len(items), 'requested_at': requested_at})
            if not has_more:
                reached_end = True
                break
            if not items:
                raise ValueError('empty page claims more data')
        except (ValueError, urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            # Preserve a clearly partial diagnostic result; never publish a complete cache on failure.
            failure = f'{type(exc).__name__}: {str(exc)[:200]}'
            break
    slug_ids = {}
    for row in rows.values():
        slug_ids.setdefault(row['full_name'].lower(), set()).add(row['repo_id'])
    ambiguous_slugs = sum(len(ids) > 1 for ids in slug_ids.values())
    if ambiguous_slugs:
        warnings.append(f'{ambiguous_slugs} repository slugs have conflicting provider IDs; identities not merged')
    if duplicates:
        warnings.append(f'{duplicates} duplicate provider IDs across pages; {"best-ranked" if keep_first else "latest"} observed row retained')
    if reached_end and first_page == 1 and (len(rows) != first_total):
        warnings.append(f'unique rows {len(rows)} do not match initial provider total {first_total}')
    status = 'inconsistent' if warnings else ('complete' if reached_end and failure is None else 'partial')
    if failure:
        warnings.append(failure)
    times = sorted((p['requested_at'] for p in pages), key=lambda t: dt.datetime.fromisoformat(t))
    return {'provider': 'gittrend', 'period': period, 'fetched_at': times[-1] if times else started,
            'fetched_started_at': times[0] if times else started, 'operation_finished_at': utcnow(),
            'cached_pages_reused': getattr(client, 'cached_pages', 0), 'source_as_of': None, 'source_as_of_status': 'UNKNOWN',
            'source_url': BASE_URL, 'metric': 'provider-reported period gain (starsToday), not independently reconstructed',
            'license': 'Provider declares rankings/aggregate data CC0; repository metadata retains its owners rights',
            'coverage': {'status': status, 'pages_fetched': len(pages), 'reported_total': first_total,
                         'last_reported_total': pages[-1]['total'] if pages else None,
                         'unique_repos': len(rows), 'duplicates': duplicates, 'reached_end': reached_end,
                         'limit': limit, 'max_pages': max_pages, 'warnings': warnings,
                         'failure': failure, 'exhaustive_github': False,
                         'scope': 'This provider period feed only; period universes differ; not all GitHub repositories',
                         'snapshot_consistency': 'Not established: provider supplies no dated revision token for paginated requests'},
            'pages': pages, 'rows': list(rows.values())}


class FeedStore:
    """Standalone provider cache. Refuses original-collector or gitpulse databases."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        expected = {'gittrend_runs', 'gittrend_rows'}
        if names - expected or (names and names != expected):
            self.conn.close()
            raise ValueError('refusing non-GitTrend database; use a separate provider cache')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS gittrend_runs(run_id INTEGER PRIMARY KEY, period TEXT NOT NULL, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS gittrend_rows(run_id INTEGER NOT NULL, repo_id INTEGER NOT NULL, row_json TEXT NOT NULL,
            PRIMARY KEY(run_id,repo_id));
        ''')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()

    def save(self, result):
        if result.get('provider') != 'gittrend' or result.get('period') not in PERIODS.values():
            raise ValueError('cannot mix another provider or period into GitTrend cache')
        metadata = {k: v for k, v in result.items() if k != 'rows'}
        with self.conn:
            cur = self.conn.execute('INSERT INTO gittrend_runs(period,metadata) VALUES (?,?)',
                                    (result['period'], json.dumps(metadata)))
            rid = cur.lastrowid
            self.conn.executemany('INSERT INTO gittrend_rows VALUES (?,?,?)',
                                 [(rid, row['repo_id'], json.dumps(row)) for row in result['rows']])
        return rid

    def latest(self, period):
        runs = list(self.conn.execute('SELECT run_id,metadata FROM gittrend_runs WHERE period=? ORDER BY run_id DESC',
                                      (period,)))
        if not runs:
            raise ValueError(f'no cached GitTrend {period} feed; run feed-fetch first')
        parsed = [(r[0], json.loads(r[1])) for r in runs]
        selected = next((r for r in parsed if r[1]['coverage']['status'] == 'complete'), parsed[0])
        result = selected[1]
        result['run_id'] = selected[0]
        if selected[0] != parsed[0][0]:
            result['latest_attempt'] = {'run_id': parsed[0][0], 'fetched_at': parsed[0][1]['fetched_at'],
                                        'coverage': parsed[0][1]['coverage']}
            result['refresh_warning'] = 'Newest refresh was not complete; showing the last complete cached feed with its original fetch time'
        result['rows'] = [json.loads(r[0]) for r in self.conn.execute('SELECT row_json FROM gittrend_rows WHERE run_id=?', (selected[0],))]
        return result

    def report(self, sort='7d', period=None, top=20, today=None):
        if sort not in ('stars', *PERIODS) or top < 1:
            raise ValueError('sort must be stars, 1d, 7d or 30d; top must be positive')
        expected_period = PERIODS.get(sort)
        if expected_period and period is not None and period != expected_period:
            raise ValueError('requested gain window does not match provider feed period')
        period = expected_period or period or 'month'
        feed = self.latest(period)
        field = 'stars' if sort == 'stars' else 'gain'
        known = sorted((r for r in feed['rows'] if r[field] is not None),
                       key=lambda r: (-r[field], -r['stars'], r['repo_id']))
        unknown = sorted((r for r in feed['rows'] if r[field] is None), key=lambda r: (-r['stars'], r['repo_id']))
        fetched_day = dt.datetime.fromisoformat(feed['fetched_at']).astimezone(dt.timezone.utc).date()
        today = today or dt.datetime.now(dt.timezone.utc).date()
        if isinstance(today, str):
            today = dt.date.fromisoformat(today)
        return {**{k: v for k, v in feed.items() if k not in ('rows', 'pages')}, 'sort': sort,
                'top': top, 'ranked': known[:top], 'unknown_candidates': unknown[:top],
                'known_count': len(known), 'unknown_count': len(unknown),
                'cache_stale_days': (today - fetched_day).days,
                'cache_stale': (today - fetched_day).days > 0,
                'freshness_note': 'Provider snapshot date UNKNOWN; cache retrieval age does not establish data freshness',
                'ranking_scope': 'Retrieved rows only' if feed['coverage']['status'] != 'complete' else 'Entire fetched provider period feed, not all GitHub'}


def render_feed_report(report):
    lines = [f"Provider: {report['provider']} | period: {report['period']} | sort: {report['sort']}",
             f"Source snapshot date: UNKNOWN | fetched: {report['fetched_started_at']} .. {report['fetched_at']}",
             f"Coverage: {report['coverage']['status']} | {report['coverage']['unique_repos']}/{report['coverage']['reported_total']} unique repos | {report['ranking_scope']}",
             f"Cache stale: {report['cache_stale']} | {report['freshness_note']}",
             'Metric: ' + report['metric'], '#\trepository\ttotal stars\tperiod gain\tlink']
    for rank, row in enumerate(report['ranked'], 1):
        gain = 'UNKNOWN' if row['gain'] is None else str(row['gain'])
        lines.append(f"{rank}\t{row['full_name']}\t{row['stars']}\t{gain}\t{row['url']}")
    lines.append(f"Unknown requested metric: {report['unknown_count']} repositories (not zero-filled)")
    if report.get('refresh_warning'):
        lines.append('WARNING: ' + report['refresh_warning'])
    for warning in report['coverage']['warnings']:
        lines.append('WARNING: ' + warning)
    return '\n'.join(lines)
