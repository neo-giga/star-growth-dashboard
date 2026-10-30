"""A network-free GitHub for the capture tests: a synthetic set of repos with per-day star arrivals, served through the same
three APIs the capture uses (REST search, GraphQL nodes(ids:), REST stargazers/history) with the real response shapes.

Stars arrive uniformly through each Pacific day, so a count at an instant is the complete earlier days plus that fraction
of the current day; history pages bucket the same arrivals by Pacific day, 30 weeks per page, newest first, the current
day partial. search lists by stars at the world's `now`, 100 per page, 1,000 per query like the real API.
"""
from __future__ import annotations
import datetime as dt, json, math, random, re, time, urllib.parse
from capture.history import PACIFIC, pacific_date

BASE = dt.date(2024, 1, 7)  # a Sunday; day offsets count from here


def make_world(repos=400, seed=1, days=800, now=None):
    rng = random.Random(seed)
    world = World(now or dt.datetime(2026, 3, 10, 5, 30, tzinfo=dt.timezone.utc))
    for n in range(repos):
        rate = math.exp(rng.uniform(math.log(0.5), math.log(400)))  # stars per day, log-uniform
        first = rng.randrange(0, days - 30)
        arrivals = [0] * days
        for d in range(first, days):
            arrivals[d] = max(0, int(rng.gauss(rate, rate / 2)))
        world.add(f'R_{n:05d}', f'owner{n % 37}/repo{n}', arrivals, language=rng.choice(['Python', 'Rust', None, 'TypeScript']),
                  created=(BASE + dt.timedelta(days=first)).isoformat() + 'T00:00:00Z')
    return world


class World:
    def __init__(self, now):
        self.now = now
        self.repos = {}
        self.requests = []
        self.search_down = False
        self.flaky = {}  # url substring -> remaining 502s to answer before serving
        self.max_per_page = None  # search answers 502 for larger pages (the degraded state measured 2026-10-29)
        self.incomplete = set()  # search URL substrings answered 200 with incomplete_results: true (a timed-out search)
        self.remaining = {'core': 5000, 'search': 30, 'graphql': 5000}

    def add(self, node_id, full_name, arrivals, language=None, created='2024-01-07T00:00:00Z', fork=False):
        prefix = [0]
        for n in arrivals:
            prefix.append(prefix[-1] + n)
        self.repos[node_id] = {'node_id': node_id, 'full_name': full_name, 'arrivals': arrivals, 'prefix': prefix, 'language': language, 'created_at': created, 'gone': False,
                               'fork': fork}

    # -- truth -------------------------------------------------------------------------------------------------------
    @staticmethod
    def day_fraction(instant):
        local = instant.astimezone(PACIFIC)
        return (local.hour * 3600 + local.minute * 60 + local.second) / 86400

    def day_count(self, node_id, date, instant=None):
        """Stars given on a Pacific date; the current date (of instant) is partial."""
        repo = self.repos[node_id]
        offset = (date - BASE).days
        if offset < 0 or offset >= len(repo['arrivals']):
            return 0
        if instant is not None:
            today = pacific_date(instant)
            if date > today:
                return 0
            if date == today:
                return int(self.day_fraction(instant) * repo['arrivals'][offset])
        return repo['arrivals'][offset]

    def stars(self, node_id, instant):
        repo = self.repos[node_id]
        today = pacific_date(instant)
        offset = (today - BASE).days
        complete = repo['prefix'][max(0, min(offset, len(repo['arrivals'])))]
        return complete + self.day_count(node_id, today, instant)

    def gained(self, node_id, first, last, instant):
        """Stars given from Pacific date `first` through `last` inclusive, as of instant."""
        return sum(self.day_count(node_id, first + dt.timedelta(days=k), instant) for k in range((last - first).days + 1))

    def history_weeks(self, node_id, instant):
        repo = self.repos[node_id]
        first_offset = next((i for i, n in enumerate(repo['arrivals']) if n), 0)
        first_sunday = BASE + dt.timedelta(days=first_offset - (first_offset % 7))
        today = pacific_date(instant)
        this_sunday = today - dt.timedelta(days=(today.weekday() + 1) % 7)
        weeks = []
        sunday = this_sunday
        while sunday >= first_sunday:
            days = [self.day_count(node_id, sunday + dt.timedelta(days=i), instant) for i in range(7)]
            weeks.append({'week': int(dt.datetime(sunday.year, sunday.month, sunday.day, tzinfo=dt.timezone.utc).timestamp()), 'total': sum(days), 'days': days})
            sunday -= dt.timedelta(days=7)
        return weeks

    # -- transport ---------------------------------------------------------------------------------------------------
    def _headers(self, resource, extra=None):
        self.remaining[resource] = max(0, self.remaining[resource] - 1)
        headers = {'x-ratelimit-resource': resource, 'x-ratelimit-remaining': str(self.remaining[resource]), 'x-ratelimit-limit': '5000',
                   'x-ratelimit-reset': str(int(time.time()) + 60)}
        headers.update(extra or {})
        return headers

    def fetch(self, method, url, body, headers):
        self.requests.append((method, url, self.now))
        for needle, left in list(self.flaky.items()):
            if needle in url and left > 0:
                self.flaky[needle] = left - 1
                return 502, {}, b'<html>502 Bad Gateway</html>'
        if '/search/repositories' in url:
            return self._search(url)
        if url.endswith('/graphql'):
            return self._graphql(json.loads(body))
        match = re.search(r'/repos/([^/]+/[^/]+)/stargazers/history\?(.*)$', url)
        if match:
            return self._history(match[1], urllib.parse.parse_qs(match[2]))
        return 404, self._headers('core'), b'{"message":"Not Found"}'

    def _search(self, url):
        if self.search_down:
            return 502, {}, b'<html>502 Bad Gateway</html>'
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        q, forks = re.subn(r' fork:(true|only)$', '', params['q'][0])  # like the API: forks are left out unless fork:true (or fork:only) is given
        forks = re.search(r'fork:(true|only)$', params['q'][0])[1] if forks else None
        page, per_page = int(params.get('page', ['1'])[0]), int(params.get('per_page', ['30'])[0])
        if self.max_per_page is not None and per_page > self.max_per_page:
            return 502, self._headers('search'), b'{"message":"Server Error"}'
        if match := re.fullmatch(r'stars:(\d+)\.\.(\d+)', q):
            lo, hi = int(match[1]), int(match[2])
        elif match := re.fullmatch(r'stars:>=(\d+)', q):
            lo, hi = int(match[1]), None
        elif match := re.fullmatch(r'stars:>(\d+)', q):
            lo, hi = int(match[1]) + 1, None
        else:
            return 422, self._headers('search'), b'{"message":"bad query"}'
        rows = []
        for repo in self.repos.values():
            if repo['gone'] or (repo['fork'] and forks is None) or (not repo['fork'] and forks == 'only'):
                continue
            stars = self.stars(repo['node_id'], self.now)
            if stars >= lo and (hi is None or stars <= hi):
                rows.append((stars, repo))
        rows.sort(key=lambda r: (r[0], r[1]['node_id']))
        if (page - 1) * per_page >= 1000:
            return 422, self._headers('search'), b'{"message":"Only the first 1000 search results are available"}'
        items = [{'node_id': r['node_id'], 'full_name': r['full_name'], 'stargazers_count': s, 'language': r['language'], 'created_at': r['created_at']}
                 for s, r in rows[(page - 1) * per_page:page * per_page]]
        incomplete = any(needle in url for needle in self.incomplete)
        return 200, self._headers('search'), json.dumps({'total_count': len(rows), 'incomplete_results': incomplete, 'items': items}).encode()

    def _graphql(self, body):
        ids = body['variables']['ids']
        nodes = []
        for node_id in ids:
            repo = self.repos.get(node_id)
            if not repo or repo['gone']:
                nodes.append(None)
            else:
                nodes.append({'id': node_id, 'nameWithOwner': repo['full_name'], 'stargazerCount': self.stars(node_id, self.now), 'createdAt': repo['created_at'],
                              'primaryLanguage': {'name': repo['language']} if repo['language'] else None})
        return 200, self._headers('graphql'), json.dumps({'data': {'rateLimit': {'cost': 1, 'remaining': self.remaining['graphql']}, 'nodes': nodes}}).encode()

    def _history(self, full_name, params):
        repo = next((r for r in self.repos.values() if r['full_name'] == full_name and not r['gone']), None)
        if repo is None:
            return 404, self._headers('core'), b'{"message":"Not Found"}'
        page, per_page = int(params.get('page', ['1'])[0]), int(params.get('per_page', ['30'])[0])
        weeks = self.history_weeks(repo['node_id'], self.now)
        chunk = weeks[(page - 1) * per_page:page * per_page]
        link = f'<https://api.github.com/repos/{full_name}/stargazers/history?page={page + 1}>; rel="next"' if page * per_page < len(weeks) else ''
        return 200, self._headers('core', {'link': link} if link else None), json.dumps(chunk).encode()


class FakeClock:
    """Monotonic seconds that only advance when something sleeps (and by a per-request tick), so paced code runs instantly."""
    def __init__(self, start=1000.0, tick=0.0):
        self.t = start
        self.tick = tick
        self.sleeps = []

    def __call__(self):
        self.t += self.tick
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
