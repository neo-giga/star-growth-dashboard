"""Each cohort repo's description and GitHub topics, read a slice a day by GraphQL nodes(ids:), apart from the counts.

The step runs after the count snapshot and the history step, and nothing it does (an error, a rate wait, its deadline) changes
the counts, the capture's completeness or exit code, or the publication: the caller wraps it, and a stop defers the rest of
the queue to the next day. Files (docs/data-layout.md):

metadata/shards/XX.json   {"schema": 1, "repos": {node_id: {description, topics, topics_total, changed, status}}}, 256 shards by
                          SHA-1 of the ID; a shard is rewritten only when a record's content (description, topics, topics_total,
                          status) changes, so an unchanged repo costs no blob
metadata/checked.json     {"schema": 1, "checked": {node_id: "YYYY-MM-DD"}}: the last successful check of each repo

Queue: cohort repos never fetched first (newest first_seen first), then those whose last check is at least RECHECK_DAYS old,
oldest first. A failed read leaves the record and its checked date as they were (the repo stays at its place in the queue).
Bounded by a query budget per day, a deadline, and a points-per-hour plan: the effective hourly limit is the lower of the
plan and the limit GitHub reports in `rateLimit`, and a query starts only when it fits in what is left of it.
"""
from __future__ import annotations
import datetime as dt, json, time
from .github import GitHubError
from .store import dumps, shard_of, write_atomic

SCHEMA = 1
TOPICS = 20  # repositoryTopics(first:); topics_total shows any truncation
QUERY = '''query($ids: [ID!]!) {
  rateLimit { cost limit remaining resetAt }
  nodes(ids: $ids) { ... on Repository { id description repositoryTopics(first: %d) { totalCount nodes { topic { name } } } } }
}''' % TOPICS
RATE_QUERY = 'query { rateLimit { cost limit remaining resetAt } }'
BATCH = 100
MAX_QUERIES = 300  # per day: steady state needs ~25 (67k / 30 days + entrants); day one fills ~30k repos, coverage completes in ~3 days
POINTS_PER_HOUR = 1000  # the plan; GitHub's reported limit applies when lower
RESERVE_POINTS = 50  # left in the hour for anything else on the token
RECHECK_DAYS = 30
MAX_FAILED_IN_A_ROW = 3  # a query shape GitHub rejects must not spend the day's budget


def classify(ids, data, errors):
    """(records, gone, unresolved) for one answer, like the count path (capture.snapshot.classify): a null node is gone only
    with no error at its path or a NOT_FOUND there; any other error, or an error without a path, leaves it unresolved."""
    nodes = data.get('nodes') or []
    at, pathless = {}, False
    for error in errors or []:
        path = error.get('path') or []
        if len(path) >= 2 and path[0] == 'nodes' and isinstance(path[1], int):
            at.setdefault(path[1], []).append(error.get('type'))
        else:
            pathless = True
    records, gone, unresolved = {}, [], []
    for index, node_id in enumerate(ids):
        node = nodes[index] if index < len(nodes) else None
        topics = (node or {}).get('repositoryTopics')
        if node and node.get('id') and isinstance(topics, dict) and index not in at:
            names = [n['topic']['name'] for n in topics.get('nodes') or [] if n and n.get('topic')]
            records[node_id] = {'description': node.get('description'), 'topics': names[:TOPICS], 'topics_total': topics.get('totalCount') or 0}
        elif node and node.get('id'):  # the repo answered, but a field of it errored: unread today, never guessed
            unresolved.append(node_id)
        elif index >= len(nodes) or pathless or any(kind != 'NOT_FOUND' for kind in at.get(index, [])):
            unresolved.append(node_id)
        else:
            gone.append(node_id)
    return records, gone, unresolved


class Store:
    def __init__(self, root):
        self.root = root / 'metadata'

    def read_shards(self):
        out = {}
        for path in sorted((self.root / 'shards').glob('*.json')):
            out.update(json.loads(path.read_bytes()).get('repos') or {})
        return out

    def read_checked(self):
        path = self.root / 'checked.json'
        return (json.loads(path.read_bytes()).get('checked') or {}) if path.exists() else {}

    def write(self, records, changed_ids, checked):
        """Rewrite the shards holding a changed record (whole, sorted), then checked.json. Returns the shards written."""
        shards = {shard_of(i): {} for i in changed_ids}
        for node_id, record in records.items():
            if (shard := shard_of(node_id)) in shards:
                shards[shard][node_id] = record
        for shard, doc in sorted(shards.items()):
            write_atomic(self.root / 'shards' / f'{shard}.json', dumps({'schema': SCHEMA, 'repos': dict(sorted(doc.items()))}))
        write_atomic(self.root / 'checked.json', dumps({'schema': SCHEMA, 'checked': dict(sorted(checked.items()))}))
        return sorted(shards)


def queue(cohort, known, checked, date, recheck_days=RECHECK_DAYS):
    """Cohort IDs in reading order: never fetched (newest first_seen first, then ID), then due (checked at least recheck_days
    ago, oldest first, then ID). Repos checked more recently are not due; repos outside the cohort are never read."""
    due_by = (date - dt.timedelta(days=recheck_days)).isoformat()
    never = sorted(i for i in cohort if i not in checked)
    never.sort(key=lambda i: (known.get(i) or {}).get('first_seen') or '', reverse=True)  # stable: ID order within a day
    due = sorted((i for i in cohort if i in checked and checked[i] <= due_by), key=lambda i: (checked[i], i))
    return never, due


def capture(gh, root, cohort, known, date, max_queries=MAX_QUERIES, points_per_hour=POINTS_PER_HOUR, deadline_seconds=None,
            recheck_days=RECHECK_DAYS, reserve=RESERVE_POINTS, wall=time.time, log=print):
    """Read the day's slice of the queue and write the metadata files. -> the diagnostic block. Raises nothing it can avoid:
    a deadline or the budget stops the reading, and what was read is written; the caller still wraps it."""
    started = time.monotonic()
    day = date.isoformat()
    cohort = set(cohort)
    store = Store(root)
    records, checked = store.read_shards(), store.read_checked()
    never, due = queue(cohort, known, checked, date, recheck_days)
    order = never + due
    report = {'asked': 0, 'checked': 0, 'changed': 0, 'failed': 0, 'gone': 0, 'queries': 0, 'points': 0, 'seconds': 0, 'deferred': 0, 'stopped': None,
              'queue': {'never_fetched': len(never), 'due': len(due)},
              'budget': {'queries_used': 0, 'queries_limit': max_queries, 'points_per_hour_plan': points_per_hour, 'points_per_hour_reported': None,
                         'points_per_hour_effective': points_per_hour, 'remaining_at_end': None, 'reserve': reserve}}
    changed_ids = set()
    rate = {}

    def observe(limit):
        if limit:
            rate.update(limit)
            report['budget']['points_per_hour_reported'] = limit.get('limit')
            report['budget']['points_per_hour_effective'] = min(points_per_hour, limit.get('limit') or points_per_hour)
            report['budget']['remaining_at_end'] = limit.get('remaining')

    def usable():
        """Points left in this hour under the effective limit: GitHub's remaining, less what lies above our plan, less the reserve."""
        if 'remaining' not in rate:
            return None
        return rate['remaining'] - max((rate.get('limit') or 0) - points_per_hour, 0) - reserve

    stop_at = None if deadline_seconds is None else gh.clock() + max(deadline_seconds, 0)
    gh.deadline = stop_at  # every request, retry and rate wait stops there
    position, failed_in_a_row, cost = 0, 0, 1
    try:
        if order and max_queries > 0:
            data = gh.graphql(RATE_QUERY)
            report['points'] += (data.get('rateLimit') or {}).get('cost') or 0
            observe(data.get('rateLimit'))
        while position < len(order) and max_queries > 0:
            if report['queries'] >= max_queries:
                report['stopped'] = 'query budget'
                break
            left = usable()
            if left is not None and left < cost:
                reset = rate.get('resetAt')
                wait = (dt.datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp() - wall() + 2) if reset else None
                if wait is None or (stop_at is not None and gh.clock() + wait > stop_at):
                    report['stopped'] = 'points: the hour is spent and resets after the deadline'
                    break
                log(f'metadata: {left} points left of {report["budget"]["points_per_hour_effective"]} this hour; waiting {wait:.0f} s for the reset')
                gh._wait(max(wait, 0), 'metadata points')
                data = gh.graphql(RATE_QUERY)
                report['points'] += (data.get('rateLimit') or {}).get('cost') or 0
                observe(data.get('rateLimit'))
                continue
            ids = order[position:position + BATCH]
            try:
                data, errors = gh.graphql(QUERY, {'ids': ids}, with_errors=True)
            except GitHubError as error:
                if error.status == 'deadline':
                    raise
                data, errors = None, error
            except Exception as error:  # noqa: BLE001 - a malformed answer fails this batch only
                data, errors = None, error
            position += len(ids)
            report['queries'] += 1
            report['asked'] += len(ids)
            if data is None:
                report['failed'] += len(ids)
                failed_in_a_row += 1
                log(f'metadata: query failed ({repr(errors)[:200]})')
                if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                    report['stopped'] = f'{failed_in_a_row} failed queries in a row'
                    break
                continue
            failed_in_a_row = 0
            limit = data.get('rateLimit') or {}
            cost = max(limit.get('cost') or 1, 1)
            report['points'] += limit.get('cost') or 0
            observe(limit)
            fresh, gone, unresolved = classify(ids, data, errors)
            report['failed'] += len(unresolved)
            report['gone'] += len(gone)
            for node_id in gone:
                old = records.get(node_id) or {'description': None, 'topics': [], 'topics_total': 0}
                fresh[node_id] = {'description': old.get('description'), 'topics': old.get('topics') or [], 'topics_total': old.get('topics_total') or 0,
                                  'status': 'gone'}  # the last known content stays; only the status says GitHub no longer answers
            for node_id, record in fresh.items():
                record.setdefault('status', 'ok')
                if (checked.get(node_id) or '') > day:  # a record newer than this run (a run stamped later): never regressed
                    continue
                report['checked'] += 1
                checked[node_id] = day
                old = records.get(node_id)
                if old is None or any(old.get(k) != record[k] for k in ('description', 'topics', 'topics_total', 'status')):
                    records[node_id] = dict(record, changed=day)
                    changed_ids.add(node_id)
    except GitHubError as error:  # the deadline (before a request, a retry or a rate wait): the rest waits for tomorrow
        report['stopped'] = repr(error)[:200]
    finally:
        gh.deadline = None
    report['deferred'] = len(order) - position
    report['changed'] = len(changed_ids)
    report['budget']['queries_used'] = report['queries']
    if report['checked']:
        report['shards_written'] = len(store.write(records, changed_ids, checked))
    else:
        report['shards_written'] = 0
    fetched = [i for i in cohort if i in records]
    report['cohort'] = len(cohort)
    report['fetched'] = len(fetched)
    report['coverage'] = round(len(fetched) / len(cohort), 4) if cohort else None
    dates = [checked[i] for i in cohort if i in checked]
    report['oldest_checked'] = min(dates) if dates else None
    report['seconds'] = round(time.monotonic() - started, 1)
    return report
