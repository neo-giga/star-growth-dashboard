"""One GitHub API client for REST, search and GraphQL, with the retry, pacing and logging every capture step needs.

Transport is injectable (fetch(method, url, body, headers) -> (status, headers, body)) so tests never touch the network.
Transient answers (5xx, timeouts, connection errors) are retried with a growing wait; a primary or secondary rate limit
(403/429) waits for retry-after or the limit reset; 4xx answers are returned to the caller as GitHubError at once.
Search is paced to at most search_per_minute requests (the API's limit is 30/min for every token); core requests can
be bounded to core_per_hour over a sliding hour (the backfill's budget), never exceeded. A `deadline` (monotonic seconds)
set on the client stops retries and pacing waits that would run past it: the request fails with status 'deadline' instead.
The client is safe to share between a few threads.
"""
from __future__ import annotations
import collections, datetime as dt, http.client, json, os, subprocess, threading, time, urllib.error, urllib.parse, urllib.request

API = 'https://api.github.com'
USER_AGENT = 'star-growth-dashboard capture (+https://github.com/neo-giga/star-growth-dashboard)'
TRANSIENT = (500, 502, 503, 504)
BACKOFF = (2, 5, 10, 20, 40, 60, 60)
# Search answers 502 from a slow backend, not from overload on our side, and the 2 s pacing already spaces requests: short waits
# (measured 2026-10-29: 8-attempt exponential waits left the workers asleep and issued 9 of the allowed 30 requests a minute).
SEARCH_BACKOFF = (2, 3, 5, 5, 10, 10, 20)
MAX_RATE_WAIT = 15 * 60
# The API allows 30 search requests per sliding minute. Pacing at exactly 2 s plus answer-time jitter brushed the limit on
# 2026-10-29 (61 answers of 403 in 1,923 requests, each costing a wait for the reset), so the default keeps a little under it.
SEARCH_PER_MINUTE = 28


class GitHubError(Exception):
    def __init__(self, status, message, url=None):
        super().__init__(f'HTTP {status} {message}' + (f' ({url})' if url else ''))
        self.status = status
        self.url = url


def token_from_environment():
    """GH_TOKEN or GITHUB_TOKEN (Actions), else the gh CLI's token (Neo's Mac)."""
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if os.environ.get(name):
            return os.environ[name]
    try:
        return subprocess.run(['gh', 'auth', 'token'], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'no GitHub token: set GH_TOKEN or log in with gh ({error})')


def urllib_fetch(method, url, body, headers):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


class GitHub:
    def __init__(self, token, fetch=urllib_fetch, sleeper=time.sleep, clock=time.monotonic, wall=time.time, max_attempts=8,
                 search_per_minute=SEARCH_PER_MINUTE, core_per_hour=None, request_log=None):
        self.token = token
        self.fetch = fetch
        self.sleeper = sleeper
        self.clock = clock
        self.wall = wall
        self.max_attempts = max_attempts
        self.search_interval = 60.0 / search_per_minute
        self.core_per_hour = core_per_hour
        self.request_log = request_log  # callable(entry dict) or None
        self.lock = threading.Lock()
        self.deadline = None
        self.last_search = None
        self.core_times = collections.deque()
        self.counts = collections.Counter()  # requests, retries, failures per resource

    # -- pacing -------------------------------------------------------------------------------------------------------
    def _wait(self, seconds, url):
        """Sleep, unless the deadline would pass first."""
        if self.deadline is not None and self.clock() + seconds > self.deadline:
            raise GitHubError('deadline', f'a {seconds:.0f} s wait would pass the deadline', url)
        self.sleeper(seconds)

    def _pace_search(self, url):
        with self.lock:
            now = self.clock()
            slot = max(now, (self.last_search or now) + self.search_interval) if self.last_search is not None else now
            self.last_search = slot
        if slot > now:
            self._wait(slot - now, url)

    def _pace_core(self, url):
        if not self.core_per_hour:
            return
        while True:
            with self.lock:
                now = self.clock()
                while self.core_times and self.core_times[0] <= now - 3600:
                    self.core_times.popleft()
                if len(self.core_times) < self.core_per_hour:
                    self.core_times.append(now)
                    return
                wait = self.core_times[0] + 3600 - now + 0.01
            self._wait(wait, url)

    def wait_core(self, n):
        """Wait until n more core requests fit in the sliding hour, without taking them: work that must not stall halfway (a
        batch's anchors and its history reads) waits here, before it starts."""
        if not self.core_per_hour:
            return
        n = min(n, self.core_per_hour)
        while True:
            with self.lock:
                now = self.clock()
                while self.core_times and self.core_times[0] <= now - 3600:
                    self.core_times.popleft()
                excess = len(self.core_times) + n - self.core_per_hour
                if excess <= 0:
                    return
                wait = self.core_times[excess - 1] + 3600 - now + 0.01
            self._wait(wait, 'core budget')

    def restore_core(self, ages):
        """Count core requests made before this client existed (each `age` seconds ago, e.g. by the run a resume continues)
        against core_per_hour, so a restart does not get a fresh hour."""
        now = self.clock()
        with self.lock:
            self.core_times.extend(sorted(now - age for age in ages if 0 <= age < 3600))
            return len(self.core_times)

    # -- requests -----------------------------------------------------------------------------------------------------
    def _request(self, method, url, body=None, resource='core', attempts=None):
        attempts = attempts or self.max_attempts
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json', 'User-Agent': USER_AGENT,
                   'X-GitHub-Api-Version': '2022-11-28'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        for attempt in range(1, attempts + 1):
            if resource == 'search':
                self._pace_search(url)
            elif resource == 'core':
                self._pace_core(url)
            started = self.clock()
            try:
                status, response_headers, data = self.fetch(method, url, body, headers)
            except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException) as error:  # reset, timeout, DNS, a truncated body
                status, response_headers, data = None, {}, repr(error).encode()
            response_headers = {k.lower(): v for k, v in response_headers.items()}
            self._log(method, url, status, response_headers, started)
            if status is not None and 200 <= status < 300:
                return status, response_headers, data
            if status in (403, 429) and self._rate_limited(response_headers, data):
                wait = self._rate_wait(response_headers)
            elif status is None or status in TRANSIENT:
                table = SEARCH_BACKOFF if resource == 'search' else BACKOFF
                wait = table[min(attempt, len(table)) - 1]
            else:
                raise GitHubError(status, data[:200].decode(errors='replace'), url)
            if attempt == attempts:
                break
            with self.lock:
                self.counts['retries'] += 1
            self._wait(wait, url)
        with self.lock:
            self.counts['failures'] += 1
        raise GitHubError(status, (data or b'')[:200].decode(errors='replace'), url)

    @staticmethod
    def _rate_limited(headers, data):
        text = data[:300].decode(errors='replace').lower()
        return headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in headers or 'rate limit' in text or 'secondary' in text

    def _rate_wait(self, headers):
        if headers.get('retry-after'):
            wait = float(headers['retry-after'])
        elif headers.get('x-ratelimit-reset'):
            wait = float(headers['x-ratelimit-reset']) - self.wall() + 2
        else:
            wait = 60
        return min(max(wait, 5), MAX_RATE_WAIT)

    def _log(self, method, url, status, headers, started):
        with self.lock:
            self.counts['requests'] += 1
        if self.request_log:
            self.request_log({'at': dt.datetime.fromtimestamp(self.wall(), dt.timezone.utc).isoformat(timespec='milliseconds'), 'method': method,
                              'url': url, 'status': status, 'resource': headers.get('x-ratelimit-resource'),
                              'remaining': headers.get('x-ratelimit-remaining'), 'ms': int((self.clock() - started) * 1000)})

    # -- public -------------------------------------------------------------------------------------------------------
    def rest(self, path, params=None, resource='core', attempts=None):
        """GET a REST path (or full URL); returns (json, headers). 4xx raise GitHubError (404 included)."""
        url = path if path.startswith('https://') else API + path
        if params:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
        _, headers, data = self._request('GET', url, resource=resource, attempts=attempts)
        return json.loads(data), headers

    def search_repositories(self, query, page=1, per_page=100, sort='stars', order='asc', attempts=None):
        data, _ = self.rest('/search/repositories', {'q': query, 'per_page': per_page, 'page': page, 'sort': sort, 'order': order}, resource='search', attempts=attempts)
        return data

    def graphql(self, query, variables=None):
        """POST a GraphQL query; returns data. Partial data with errors is returned (nodes() reports missing IDs that way)."""
        body = json.dumps({'query': query, 'variables': variables or {}}).encode()
        for attempt in range(1, self.max_attempts + 1):
            _, _, raw = self._request('POST', API + '/graphql', body, resource='graphql')
            answer = json.loads(raw)
            if answer.get('data'):
                return answer['data']
            errors = answer.get('errors') or []
            if any(e.get('type') == 'RATE_LIMITED' for e in errors) and attempt < self.max_attempts:
                with self.lock:
                    self.counts['retries'] += 1
                self._wait(60, API + '/graphql')
                continue
            raise GitHubError(200, json.dumps(errors)[:200], API + '/graphql')
        raise GitHubError(200, 'rate limited', API + '/graphql')
