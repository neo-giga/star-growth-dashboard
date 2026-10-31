"""THROWAWAY measurement, branch agent-metadata-probe only (never merged): what do descriptions and topics cost on top of the
daily capture's scalar query?

Three sequential read-only GraphQL queries on the same 100 fixture node IDs, one batch each:
  A  the capture's current query (capture/snapshot.py's node fields)
  B  A + description
  C  B + repositoryTopics(first: 5) { totalCount nodes { topic { name } } pageInfo { hasNextPage } }
Each reports rateLimit cost/limit/remaining/resetAt, elapsed time, request and response bytes, attempts, partial GraphQL errors
and the IDs that came back null or errored. Writes metrics.json and metadata.json (public repository metadata only) to --out.
Never writes the data branch or any catalog; never prints the token or request headers.
"""
from __future__ import annotations
import argparse, json, os, pathlib, re, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from capture.github import API, GitHub, GitHubError
from capture.snapshot import QUERY as CAPTURE_QUERY

MAX_ATTEMPTS = 3
PROJECTED_BATCHES = 670  # the daily capture's batches of 100 at >= 1,000 repos per window (measured ~67k ids / 100)
NODE_FIELDS = re.search(r'\.\.\. on Repository \{ (.*?) \} \}\n', CAPTURE_QUERY)[1]
TOPICS = 'repositoryTopics(first: 5) { totalCount nodes { topic { name } } pageInfo { hasNextPage } }'
RATE = 'rateLimit { cost limit remaining resetAt nodeCount }'


def query(fields):
    return 'query($ids: [ID!]!) {\n  %s\n  nodes(ids: $ids) { ... on Repository { %s } }\n}' % (RATE, fields)


QUERIES = {
    'A_scalar': query(NODE_FIELDS),
    'B_description': query(NODE_FIELDS + ' description'),
    'C_description_topics': query(NODE_FIELDS + ' description ' + TOPICS),
}


def topic_state(node):
    """unknown (no node or no field), empty, complete, truncated (more than the first 5 exist)."""
    if not node or node.get('repositoryTopics') is None:
        return 'unknown'
    topics = node['repositoryTopics']
    if (topics.get('totalCount') or 0) == 0 and not topics.get('nodes'):
        return 'empty'
    return 'truncated' if (topics.get('pageInfo') or {}).get('hasNextPage') else 'complete'


def run_one(gh, label, text, ids, clock=time.monotonic):
    body = json.dumps({'query': text, 'variables': {'ids': ids}}).encode()
    attempts = []
    gh.request_log = lambda entry: attempts.append({'status': entry['status'], 'ms': entry['ms'], 'resource': entry['resource'], 'remaining': entry['remaining']})
    started = clock()
    result = {'label': label, 'ids_asked': len(ids), 'request_bytes': len(body)}
    try:
        status, headers, raw = gh._request('POST', API + '/graphql', body, resource='graphql', attempts=MAX_ATTEMPTS)
    except GitHubError as error:
        result.update(elapsed_ms=int((clock() - started) * 1000), attempts=attempts, http_status=error.status,
                      transport_error=str(error).split(' (https://')[0][:300], response_bytes=None, rate_limit=None, errors=[],
                      nodes_returned=0, inaccessible_ids=list(ids), errored_ids=[])
        return result, {}
    result['elapsed_ms'] = int((clock() - started) * 1000)
    result.update(attempts=attempts, http_status=status, response_bytes=len(raw),
                  rate_limit_headers={k: headers.get(k) for k in ('x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-used', 'x-ratelimit-resource', 'x-ratelimit-reset')})
    try:
        answer = json.loads(raw)
    except ValueError:
        result.update(parse_error=True, rate_limit=None, errors=[], nodes_returned=0, inaccessible_ids=list(ids), errored_ids=[])
        return result, {}
    data = answer.get('data') or {}
    nodes = data.get('nodes') or []
    errors = [{'type': e.get('type'), 'path': e.get('path'), 'message': (e.get('message') or '')[:300]} for e in answer.get('errors') or []]
    errored = sorted({ids[e['path'][1]] for e in errors if isinstance(e.get('path'), list) and len(e['path']) > 1 and e['path'][0] == 'nodes'
                      and isinstance(e['path'][1], int) and e['path'][1] < len(ids)})
    by_id = {node['id']: node for node in nodes if node and node.get('id')}
    result.update(rate_limit=data.get('rateLimit'), errors=errors, nodes_returned=len(by_id),
                  inaccessible_ids=[i for i in ids if i not in by_id], errored_ids=errored, data_present=bool(answer.get('data')))
    if label != 'A_scalar':
        descriptions = [by_id[i].get('description') for i in by_id]
        result['description'] = {'present': sum(1 for d in descriptions if d), 'empty': sum(1 for d in descriptions if not d),
                                 'unknown': len(ids) - len(by_id), 'chars': sum(len(d) for d in descriptions if d)}
    if label == 'C_description_topics':
        states = {i: topic_state(by_id.get(i)) for i in ids}
        result['topics'] = {s: sum(1 for v in states.values() if v == s) for s in ('complete', 'truncated', 'empty', 'unknown')}
        result['topics']['total_count_sum'] = sum((by_id[i].get('repositoryTopics') or {}).get('totalCount') or 0 for i in by_id)
    return result, by_id


def projection(results, batches, hourly_limit):
    """The probe's cost per batch of 100 times the capture's batches; compared with the hourly limit the token reported."""
    out = {'batches': batches, 'hourly_limit': hourly_limit}
    for r in results:
        cost = (r.get('rate_limit') or {}).get('cost')
        out[r['label']] = None if cost is None else {'cost_per_batch': cost, 'projected_points': cost * batches,
                                                     'hours_of_limit': round(cost * batches / hourly_limit, 2) if hourly_limit else None}
    a, c = out.get('A_scalar'), out.get('C_description_topics')
    if not (a and c and hourly_limit):
        out['recommendation'] = 'unmeasured: a query failed or reported no cost; rerun before deciding'
    elif c['projected_points'] <= hourly_limit and c['cost_per_batch'] == a['cost_per_batch']:
        out['recommendation'] = 'topics cost no more than the scalar query: inline enrichment fits the capture budget (still a separate, reviewed change)'
    else:
        out['recommendation'] = ('topics raise the per-batch cost or exceed one hour of the limit: keep the daily capture scalar-only and enrich '
                                 'descriptions/topics in a separate cached incremental job (new or changed repos first), never by weakening the capture')
    return out


def main(argv=None, fetch=None, clock=time.monotonic, sleeper=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--out', required=True, help='a new directory for metrics.json and metadata.json')
    parser.add_argument('--projected-batches', type=int, default=PROJECTED_BATCHES)
    args = parser.parse_args(argv)
    fixture = json.loads(pathlib.Path(args.fixture).read_text())
    ids = fixture['node_ids']
    if len(ids) != 100 or len(set(ids)) != 100:
        raise SystemExit('the fixture must hold 100 distinct node IDs')
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise SystemExit('set GH_TOKEN (the Actions github.token); the probe does not fall back to a personal gh login')
    kwargs = {'fetch': fetch} if fetch else {}
    gh = GitHub(token, clock=clock, sleeper=sleeper, max_attempts=MAX_ATTEMPTS, **kwargs)
    gh.deadline = clock() + 15 * 60
    results, metadata = [], {}
    for label, text in QUERIES.items():  # sequential: one batch each, three queries in total
        result, by_id = run_one(gh, label, text, ids, clock=clock)
        results.append(result)
        if label == 'C_description_topics':
            for i in ids:
                node = by_id.get(i)
                topics = (node or {}).get('repositoryTopics') or {}
                metadata[i] = {'full_name': (node or {}).get('nameWithOwner'), 'description': (node or {}).get('description'),
                               'topics': [t['topic']['name'] for t in topics.get('nodes') or [] if t and t.get('topic')],
                               'topics_total': topics.get('totalCount'), 'topic_state': topic_state(node)}
    limits = [(r.get('rate_limit') or {}).get('limit') for r in results]
    metrics = {'schema': 'star-growth-dashboard/metadata-probe/1', 'token_class': 'actions-github-token' if os.environ.get('GITHUB_ACTIONS') == 'true' else 'local',
               'fixture_source': fixture.get('source'), 'max_attempts_per_query': MAX_ATTEMPTS, 'queries': QUERIES, 'results': results,
               'projection': projection(results, args.projected_batches, next((x for x in limits if x), None))}
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'metrics.json').write_text(json.dumps(metrics, indent=1) + '\n')
    (out / 'metadata.json').write_text(json.dumps(metadata, indent=1, ensure_ascii=False) + '\n')
    for r in results:
        print(f"{r['label']}: http {r['http_status']}, cost {(r.get('rate_limit') or {}).get('cost')}, {r.get('elapsed_ms')} ms, "
              f"{r.get('response_bytes')} bytes, {r['nodes_returned']}/{r['ids_asked']} nodes, {len(r['errors'])} errors, {len(r['attempts'])} attempts")
    print('recommendation:', metrics['projection']['recommendation'])
    return 0 if all(r['http_status'] == 200 for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
