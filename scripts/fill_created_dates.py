"""Fill missing GitHub creation dates through `gh api graphql` (GITHUB_TOKEN/gh login; no token handling here).

A date is accepted only when GitHub's databaseId agrees with the row's cached GitHub ID; rows without
a cached ID take the date and record the ID. Rows GitHub cannot resolve stay UNKNOWN.
The GITHUB_TOKEN GraphQL budget is small (assume 1,000 points per hour), so a run sends at most
MAX_QUERIES batched queries, in feed/rank order, and stops at the first rate-limit answer; the rest
stays UNKNOWN and fills on later days. Dates never fail a publish.
"""
from __future__ import annotations
import argparse, datetime as dt, json, re, subprocess
NAME = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
MAX_QUERIES = 900


class RateLimited(RuntimeError):
    """GitHub answered RATE_LIMITED: later queries in this run would only burn the remaining budget."""


def query_created_dates(names):
    """One aliased GraphQL query; returns {casefolded full_name: (databaseId, createdAt)} for resolved repos."""
    parts = []
    for index, name in enumerate(names):
        if not NAME.fullmatch(name):
            raise ValueError('unsafe repository name')
        owner, repo = name.split('/')
        parts.append(f'r{index}: repository(owner:{json.dumps(owner)}, name:{json.dumps(repo)}) {{ databaseId createdAt }}')
    body = json.dumps({'query': 'query { ' + ' '.join(parts) + ' }'})
    proc = subprocess.run(['gh', 'api', 'graphql', '--input', '-'], input=body, capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError((proc.stderr.strip() or 'gh api graphql returned no JSON')[:300]) from None
    data = payload.get('data') if isinstance(payload, dict) else None
    errors = payload.get('errors') if isinstance(payload, dict) and isinstance(payload.get('errors'), list) else []
    if any(isinstance(e, dict) and e.get('type') == 'RATE_LIMITED' for e in errors):
        raise RateLimited(str(next(e.get('message') for e in errors if isinstance(e, dict) and e.get('type') == 'RATE_LIMITED'))[:300])
    found = {}
    for index, name in enumerate(names):
        node = (data or {}).get(f'r{index}')
        if isinstance(node, dict) and type(node.get('databaseId')) is int and isinstance(node.get('createdAt'), str):
            found[name.casefold()] = (node['databaseId'], node['createdAt'])
    return found


def merge_created_dates(rows, found):
    """Fill created_at on rows lacking it from found; returns the number of rows filled."""
    filled = 0
    for row in rows:
        if row.get('created_at') is not None or not isinstance(row.get('full_name'), str):
            continue
        hit = found.get(row['full_name'].casefold())
        if not hit:
            continue
        github_id, created = hit
        metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        known_id = row.get('created_at_github_repo_id')
        if known_id is None:
            known_id = metadata.get('github_repo_id')
        if known_id is not None and known_id != github_id:
            continue
        try:
            parsed = dt.datetime.fromisoformat(created.replace('Z', '+00:00'))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        row['created_at'] = parsed.isoformat()
        row['created_at_github_repo_id'] = github_id
        filled += 1
    return filled


def fill_created_dates(data, query=query_created_dates, batch_size=100, max_queries=MAX_QUERIES):
    """Query missing names once each (batched, feed/rank order, at most max_queries batches), merge into every feed, refresh
    creation_dates counts. Names beyond the budget are deferred to a later run."""
    missing = {}
    for feed in data.get('feeds', []):
        for row in feed.get('rows', []):
            if row.get('created_at') is None and isinstance(row.get('full_name'), str):
                missing.setdefault(row['full_name'].casefold(), row['full_name'])
    names = list(missing.values())[:max_queries * batch_size]
    summary = {'requested': len(names), 'deferred': len(missing) - len(names), 'filled': 0, 'batches': 0, 'failed_batches': 0, 'errors': [], 'stopped': None}
    found = {}
    for start in range(0, len(names), batch_size):
        summary['batches'] += 1
        try:
            found.update(query(names[start:start + batch_size]))
        except RateLimited as exc:
            summary['failed_batches'] += 1
            summary['stopped'] = f'rate limited after {summary["batches"]} queries: {exc}'
            summary['deferred'] += len(names) - start - batch_size if start + batch_size < len(names) else 0
            break
        except (RuntimeError, OSError) as exc:
            summary['failed_batches'] += 1
            summary['errors'].append(str(exc)[:300])
    for feed in data.get('feeds', []):
        rows = feed.get('rows', [])
        summary['filled'] += merge_created_dates(rows, found) if found else 0
        if isinstance(feed.get('creation_dates'), dict):
            known = sum(r.get('created_at') is not None for r in rows)
            feed['creation_dates'].update(known=known, unknown=len(rows) - known)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--data', required=True, help='dashboard data.json to read')
    p.add_argument('--output', required=True, help='where to write the filled data.json')
    p.add_argument('--max-queries', type=int, default=MAX_QUERIES, help='GraphQL queries per run (100 names each)')
    a = p.parse_args()
    with open(a.data) as f:
        data = json.load(f)
    summary = fill_created_dates(data, max_queries=a.max_queries)
    with open(a.output, 'w') as f:
        f.write(json.dumps(data, separators=(',', ':')) + '\n')
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
