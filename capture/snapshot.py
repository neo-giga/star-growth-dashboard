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


def classify(ids, data, errors):
    """(rows, missing, unresolved) for one nodes(ids:) answer. A null node is confirmed missing (deleted, private or not a
    repository) only when the answer has no error at its path or a NOT_FOUND error there (GitHub's answer for an ID it cannot
    resolve, checked 2026-10-31); any other error at that path, or an error without a path while nodes are null, leaves the ID
    unresolved: unread today, never counted as gone."""
    nodes = data.get('nodes') or []
    at = {}
    pathless = False
    for error in errors or []:
        path = error.get('path') or []
        if len(path) >= 2 and path[0] == 'nodes' and isinstance(path[1], int):
            at.setdefault(path[1], []).append(error.get('type'))
        else:
            pathless = True
    rows, missing, unresolved = {}, [], []
    for index, node_id in enumerate(ids):
        node = nodes[index] if index < len(nodes) else None
        if node and node.get('id'):
            rows[node['id']] = {'full_name': node['nameWithOwner'], 'stars': node['stargazerCount'], 'created_at': node.get('createdAt'),
                                'language': (node.get('primaryLanguage') or {}).get('name')}
        elif index >= len(nodes) or pathless or any(kind != 'NOT_FOUND' for kind in at.get(index, [])):
            unresolved.append(node_id)
        else:
            missing.append(node_id)
    return rows, missing, unresolved


def read_counts(gh, node_ids, workers=4, batch=BATCH, clock=time.monotonic, failed=None):
    """-> (rows, stats): rows = {node_id: {full_name, stars, language, created_at}} for every node read; stats counts
    queries, points, failed queries (after retries), ids confirmed missing from the answer (deleted, private or not a
    repository) and unresolved ids (an error other than NOT_FOUND at their path). `failed`, a list, receives the ids of the
    failed queries and the unresolved ids: unread today, unlike a missing id the answer said is gone."""
    node_ids = list(node_ids)
    rows, stats, lock = {}, {'asked': len(node_ids), 'read': 0, 'missing': 0, 'unresolved': 0, 'queries': 0, 'points': 0, 'failed_queries': 0, 'seconds': 0}, threading.Lock()
    started = clock()

    def one(ids):
        try:
            data, errors = gh.graphql(QUERY, {'ids': ids}, with_errors=True)
        except Exception as error:  # a batch that failed after every retry: its repos stay unread today
            with lock:
                stats['failed_queries'] += 1
                stats['queries'] += 1
                if failed is not None:
                    failed.extend(ids)
            return
        got, missing, unresolved = classify(ids, data, errors)
        with lock:
            rows.update(got)
            stats['queries'] += 1
            stats['points'] += (data.get('rateLimit') or {}).get('cost') or 0
            stats['read'] += len(got)
            stats['missing'] += len(missing)
            stats['unresolved'] += len(unresolved)
            if failed is not None:
                failed.extend(unresolved)

    batches = [node_ids[i:i + batch] for i in range(0, len(node_ids), batch)]
    if workers <= 1:
        for ids in batches:
            one(ids)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, batches))
    stats['seconds'] = round(clock() - started, 1)
    return rows, stats
