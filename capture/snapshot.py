"""Today's star count of every known repo by GraphQL nodes(ids:), 100 per query, a few workers. Never search.

Measured 2026-10-29: 100 repositories per query cost 1 point and took about 3 s; 20,011 repos read in 10.5 min on one worker
with 0 HTTP failures. Node IDs survive renames; a deleted or private repo comes back as null.
"""
from __future__ import annotations
import concurrent.futures, threading, time

QUERY = '''query($ids: [ID!]!) {
  rateLimit { cost remaining }
  nodes(ids: $ids) { ... on Repository { id nameWithOwner stargazerCount createdAt primaryLanguage { name } } }
}'''
BATCH = 100


def read_counts(gh, node_ids, workers=4, batch=BATCH, clock=time.monotonic, failed=None):
    """-> (rows, stats): rows = {node_id: {full_name, stars, language, created_at}} for every node read; stats counts
    queries, points, failed queries (after retries) and ids missing from the answer (deleted, private or not a repository).
    `failed`, a list, receives the ids of the failed queries: unread today, unlike a missing id the answer said is null."""
    node_ids = list(node_ids)
    rows, stats, lock = {}, {'asked': len(node_ids), 'read': 0, 'missing': 0, 'queries': 0, 'points': 0, 'failed_queries': 0, 'seconds': 0}, threading.Lock()
    started = clock()

    def one(ids):
        try:
            data = gh.graphql(QUERY, {'ids': ids})
        except Exception as error:  # a batch that failed after every retry: its repos stay unread today
            with lock:
                stats['failed_queries'] += 1
                stats['queries'] += 1
                if failed is not None:
                    failed.extend(ids)
            return
        got = {}
        for node in data.get('nodes') or []:
            if node and node.get('id'):
                got[node['id']] = {'full_name': node['nameWithOwner'], 'stars': node['stargazerCount'], 'created_at': node.get('createdAt'),
                                   'language': (node.get('primaryLanguage') or {}).get('name')}
        with lock:
            rows.update(got)
            stats['queries'] += 1
            stats['points'] += (data.get('rateLimit') or {}).get('cost') or 0
            stats['read'] += len(got)
            stats['missing'] += len(ids) - len(got)

    batches = [node_ids[i:i + batch] for i in range(0, len(node_ids), batch)]
    if workers <= 1:
        for ids in batches:
            one(ids)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, batches))
    stats['seconds'] = round(clock() - started, 1)
    return rows, stats
