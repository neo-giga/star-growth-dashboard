/* Momentum, the static star-growth explorer: every repository with at least min_stars stars in one table, sortable by creation date, stars or any gain window; repository details come live from the unauthenticated GitHub API. */
'use strict';
const PAGE_SIZE = 50;
const DATA_FILE = 'data.json';
const DATA_SCHEMA = 'star-growth-dashboard/page/1';
const UNKNOWN = '—';
const DEFAULT_SORT = 'gain_7d';
/* Sortable columns in table order: key, header label, kind. Repository and language are not sortable (see RETIRED_SORTS). */
const COLUMNS = [
  ['created_at', 'Created', 'time'], ['stars', 'Stars', 'number'],
  ['gain_1d', '1 day', 'gain'], ['gain_7d', '1 week', 'gain'], ['gain_15d', '15 days', 'gain'], ['gain_30d', '1 month', 'gain'],
  ['gain_90d', '3 months', 'gain'], ['gain_180d', '6 months', 'gain'], ['gain_365d', '1 year', 'gain']
];
/* Sort keys older links may carry (the page once sorted by them): they fall back to the default sort silently, with no "ignored" note. */
const RETIRED_SORTS = ['full_name', 'language'];
const GAIN_COLUMNS = COLUMNS.filter(([, , kind]) => kind === 'gain').map(([key]) => key);
const GITHUB_COUNTS = [
  ['contributors', 'Contributors', 'contributors?per_page=1&anon=true'],
  ['issue_comments', 'Issue comments', 'issues/comments?per_page=1'],
  ['pr_review_comments', 'PR review comments', 'pulls/comments?per_page=1']
];
function numeric(value) {
  if (typeof value === 'string' && /^[+-]?\d+$/.test(value)) value = Number(value);
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
}
function creationTime(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute, second] = match.map(Number);
  if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate() || hour > 23 || minute > 59 || second > 59) return null;
  const instant = Date.parse(value);
  return Number.isFinite(instant) ? instant : null;
}
function creationLabel(value) {
  const instant = creationTime(value);
  return instant === null ? UNKNOWN : new Date(instant).toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC'});
}
function repoURL(name) {
  return typeof name === 'string' && /^[\w.-]+\/[\w.-]+$/.test(name) ? `https://github.com/${name}` : null;
}
function displayNumber(value, gain = false) {
  const n = numeric(value);
  return n === null ? UNKNOWN : `${gain && n > 0 ? '+' : ''}${n.toLocaleString('en-US')}`;
}
function timeLabel(value) {
  if (!value || Number.isNaN(Date.parse(value))) return UNKNOWN;
  return new Date(value).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
}
function clockLabel(value) {
  const instant = typeof value === 'number' ? value : Date.parse(value);
  return Number.isFinite(instant) ? new Date(instant).toISOString().slice(11, 16) + ' UTC' : UNKNOWN;
}
/* The columnar page file as rows: {rows, windows, generated_at, date, threshold, row_count}, or an Error. Each row carries its search key
   (lower-cased name) precomputed; the creation instant and UTC date are parsed once per row, the first time a sort or filter needs them. */
function parseData(data) {
  if (!data || typeof data !== 'object' || data.schema !== DATA_SCHEMA || !data.columns || typeof data.columns !== 'object' || !Array.isArray(data.windows)) throw new Error('The local data file has an invalid format.');
  const count = numeric(data.row_count), columns = data.columns;
  const names = ['full_name', 'language', 'created_at', 'stars', ...GAIN_COLUMNS, 'history_mask'];
  if (count === null || count < 0 || names.some(name => !Array.isArray(columns[name]) || columns[name].length !== count)) throw new Error('The local data file has an invalid format.');
  const windows = GAIN_COLUMNS.map(key => data.windows.find(w => w && w.column === key) || {column: key, source: null, exact_since: null});
  const rows = new Array(count);
  for (let i = 0; i < count; i++) {
    const row = {full_name: columns.full_name[i], language: columns.language[i], created_at: columns.created_at[i], stars: columns.stars[i], history_mask: numeric(columns.history_mask[i]) || 0,
      pending: Array.isArray(columns.history_pending) && (columns.history_pending[i] === 1 || columns.history_pending[i] === 2), retrying: Array.isArray(columns.history_pending) && columns.history_pending[i] === 2};
    for (const key of GAIN_COLUMNS) row[key] = columns[key][i];
    row.search = String(row.full_name || '').toLowerCase();
    rows[i] = row;
  }
  const threshold = numeric(data.threshold), pending = rows.reduce((n, row) => n + (row.pending ? 1 : 0), 0), retrying = rows.reduce((n, row) => n + (row.retrying ? 1 : 0), 0);
  return {rows, windows, generated_at: data.generated_at, date: data.date, threshold, min_stars: numeric(data.min_stars) ?? (threshold === null ? null : threshold + 1), row_count: count,
    pending, retrying, capture: data.capture && typeof data.capture === 'object' ? data.capture : null};
}
/* One line on how complete the capture is; null when the file says nothing about it. */
function captureNote(data) {
  const parts = [], capture = data && data.capture;
  if (capture && capture.status === 'complete') parts.push('Capture complete: every discovery slice listed and every count read');
  else if (capture && capture.status) parts.push(`Capture partial: ${Array.isArray(capture.reasons) && capture.reasons.length ? capture.reasons.join('; ') : 'see the capture diagnostic'}`);
  if (capture && numeric(capture.search_total) !== null) parts.push(`${displayNumber(capture.rows)} rows vs GitHub search total ${displayNumber(capture.search_total)}`);
  return parts.length ? parts.join(' · ') + '.' : null;
}
/* The hover text of a gain shown as — because the repository's history is pending: why, and that the gain arrives once its history is read. */
function pendingTitle(retrying = false) {
  const why = retrying ? 'a read of this repository\'s GitHub star history failed or came back incomplete; it is retried in turn' : 'this repository joined recently and its GitHub star history is read gradually';
  return `Not known yet: ${why}. Its stars are current; this gain appears once its history is read.`;
}
/* The line above the table while any row's history is pending: how many rows, and that their gains appear once their history is read; null when nothing is pending. */
function pendingNote(data) {
  if (!data || !data.pending) return null;
  const retried = data.retrying ? ` (${displayNumber(data.retrying)} retried after a failed read)` : '';
  return `Gains for ${displayNumber(data.pending)} recently added repositories${retried} aren't known yet; they appear once their star history is read.`;
}
function windowTooltip(window) {
  return `From GitHub's star history; exact daily snapshots from ${window && window.exact_since ? window.exact_since : UNKNOWN}`;
}
/* A window whose snapshot ring is deep enough (date on or after exact_since) but read no baseline: the capture N days ago was missed. */
function windowNote(window, date) {
  const days = window ? numeric(window.days) : null;
  if (days === null || window.baseline_date != null || typeof window.exact_since !== 'string' || typeof date !== 'string' || date < window.exact_since) return null;
  return `Not available today: the capture ${days} day${days === 1 ? '' : 's'} ago was missed`;
}
/* Filters, with GitHub's advanced-search meaning (language:, created:, stars:), AND-combined. created compares the UTC date of GitHub's
   creation instant; bounds are inclusive; a row whose language, creation date or stars are unknown never matches a bound or a specific
   language, and only UNKNOWN_LANGUAGE matches a null language. */
const UNKNOWN_LANGUAGE = 'Unknown';
const NO_FILTERS = Object.freeze({search: '', language: '', created: Object.freeze({after: null, before: null}), stars: Object.freeze({min: null, max: null})});
/* 'YYYY-MM-DD' when value is a real calendar date, else null. */
function utcDay(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const instant = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(instant) && new Date(instant).toISOString().slice(0, 10) === value ? value : null;
}
function shiftDay(day, days) { return new Date(Date.parse(`${day}T00:00:00Z`) + days * 864e5).toISOString().slice(0, 10); }
function starCount(value) { return typeof value === 'string' && /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null; }
/* GitHub qualifier value syntax for one inclusive range: >=X, >X, <=X, <X, X..Y (either side may be *) or X alone; {low, high} (null = open), or
   null when the text is not one of those. */
function parseRange(text, parse, step) {
  const value = typeof text === 'string' ? text.trim() : '';
  let match = /^(>=|<=|>|<)(.+)$/.exec(value);
  if (match) {
    const bound = parse(match[2]);
    if (bound === null) return null;
    return {'>=': {low: bound, high: null}, '<=': {low: null, high: bound}, '>': {low: step(bound, 1), high: null}, '<': {low: null, high: step(bound, -1)}}[match[1]];
  }
  match = /^([^.]+)\.\.([^.]+)$/.exec(value);
  if (match) {
    const low = match[1] === '*' ? null : parse(match[1]), high = match[2] === '*' ? null : parse(match[2]);
    return (low === null && match[1] !== '*') || (high === null && match[2] !== '*') || (low === null && high === null) ? null : {low, high};
  }
  const bound = parse(value);
  return bound === null ? null : {low: bound, high: bound};
}
function parseCreated(text) { const range = parseRange(text, utcDay, shiftDay); return range && {after: range.low, before: range.high}; }
function parseStars(text) { const range = parseRange(text, starCount, (n, d) => n + d); return range && {min: range.low, max: range.high}; }
/* The qualifier value for an inclusive range: '' (no bound), '>=low', '<=high' or 'low..high'. */
function formatRange(low, high) {
  if (low === null && high === null) return '';
  return high === null ? `>=${low}` : low === null ? `<=${high}` : `${low}..${high}`;
}
/* The UTC date a row was created, computed once per row. */
function createdDay(row) {
  if (row.created_day === undefined) { const instant = sortValue(row, 'created_at'); row.created_day = instant === null ? null : new Date(instant).toISOString().slice(0, 10); }
  return row.created_day;
}
function filterRows(rows, {search = '', language = '', created = NO_FILTERS.created, stars = NO_FILTERS.stars} = {}) {
  const query = search.trim().toLowerCase(), after = created.after, before = created.before, min = stars.min, max = stars.max;
  const dated = after !== null || before !== null, counted = min !== null || max !== null;
  return rows.filter(row => {
    if (language && (language === UNKNOWN_LANGUAGE ? !!row.language : row.language !== language)) return false;
    if (query && !(row.search || String(row.full_name || '').toLowerCase()).includes(query)) return false;
    if (dated) { const day = createdDay(row); if (day === null || (after !== null && day < after) || (before !== null && day > before)) return false; }
    if (counted) { const n = numeric(row.stars); if (n === null || (min !== null && n < min) || (max !== null && n > max)) return false; }
    return true;
  });
}
/* One page of an already filtered and sorted list: the page number is clamped to [1, pages]. */
function pageOf(rows, page, size = PAGE_SIZE) {
  const pages = Math.max(1, Math.ceil(rows.length / size)), current = Math.min(Math.max(1, page), pages), offset = (current - 1) * size;
  return {page: current, pages, offset, rows: rows.slice(offset, offset + size)};
}
/* URL state: ?q=&language=&created=&stars=&sort=&dir=&page=, created and stars in GitHub's qualifier value syntax. Unknown parameters are
   ignored; a known one with an invalid value is dropped and listed in invalid as [parameter, value, what it should be]. */
const QUERY_HELP = {created: 'a UTC date or range such as >=2024-01-01, <=2024-12-31 or 2024-01-01..2024-12-31', stars: 'a star count or range such as >=10000 or 1000..5000',
  sort: 'a column name', dir: 'asc or desc', page: 'a page number', language: 'a language in this dataset'};
function parseQuery(search) {
  const params = new URLSearchParams(search || ''), invalid = [], get = key => { const value = params.get(key); return value === null || value === '' ? null : value; };
  const state = {...NO_FILTERS, sort: DEFAULT_SORT, direction: 'desc', page: 1};
  if (get('q') !== null) state.search = get('q');
  if (get('language') !== null) state.language = get('language');
  for (const [key, parse] of [['created', parseCreated], ['stars', parseStars]]) {
    if (get(key) === null) continue;
    const value = parse(get(key));
    if (value) state[key] = value; else invalid.push([key, get(key), QUERY_HELP[key]]);
  }
  if (get('sort') !== null) { if (COLUMNS.some(([key]) => key === get('sort'))) state.sort = get('sort'); else if (!RETIRED_SORTS.includes(get('sort'))) invalid.push(['sort', get('sort'), QUERY_HELP.sort]); }
  if (get('dir') !== null) { if (get('dir') === 'asc' || get('dir') === 'desc') state.direction = get('dir'); else invalid.push(['dir', get('dir'), QUERY_HELP.dir]); }
  if (get('page') !== null) { const page = starCount(get('page')); if (page) state.page = page; else invalid.push(['page', get('page'), QUERY_HELP.page]); }
  return {state, invalid};
}
function serializeQuery(state) {
  const pairs = [['q', (state.search || '').trim()], ['language', state.language || ''], ['created', formatRange(state.created.after, state.created.before)],
    ['stars', formatRange(state.stars.min, state.stars.max)], ['sort', state.sort === DEFAULT_SORT ? '' : state.sort], ['dir', state.direction === 'asc' ? 'asc' : ''],
    ['page', state.page > 1 ? String(state.page) : '']];
  const text = pairs.filter(([, value]) => value).map(([key, value]) => `${key}=${encodeURIComponent(value).replace(/%3D/g, '=').replace(/%3A/g, ':').replace(/%2F/g, '/').replace(/%2C/g, ',')}`).join('&');
  return text ? `?${text}` : '';
}
/* The visible note for parameters dropped from a link, or null. */
function invalidNote(invalid) {
  if (!invalid.length) return null;
  return `Ignored from this link: ${invalid.map(([key, value, help]) => `${key}=${value} (expected ${help})`).join('; ')}. The other filters still apply.`;
}
/* The first render's state when the visitor touched the form while the data loaded: the link's state itself (page included) when search,
   created, stars, sort and order are unchanged; otherwise the link's state (its language included: none can be chosen before the options
   exist) with the visitor's values, from page 1. */
function keepEarlyEdits(fromURL, edits) {
  const range = (bounds, low, high) => formatRange(bounds[low], bounds[high]);
  if (edits.search === fromURL.search && range(edits.created, 'after', 'before') === range(fromURL.created, 'after', 'before') && range(edits.stars, 'min', 'max') === range(fromURL.stars, 'min', 'max') &&
    edits.sort === fromURL.sort && edits.direction === fromURL.direction) return fromURL;
  return {...fromURL, search: edits.search, created: edits.created, stars: edits.stars, sort: edits.sort, direction: edits.direction, page: 1};
}
/* Active filters as [key, qualifier text] pairs, in the URL's order; the chips render these. */
function activeFilters(state) {
  return [['q', (state.search || '').trim()], ['language', state.language || ''], ['created', formatRange(state.created.after, state.created.before)],
    ['stars', formatRange(state.stars.min, state.stars.max)]].filter(([, value]) => value);
}
/* A row's value for a sort key: null (an unknown date or count, a gain whose history is pending) sorts last in both directions. */
function sortValue(row, key) {
  if (key === 'created_at') { if (row.created === undefined) row.created = creationTime(row.created_at); return row.created; }
  return numeric(row[key]);
}
/* Filter first, then sort the whole filtered list (pagination slices the result): a filter never applies to a pre-cut list. */
function selectRows(rows, {sort = DEFAULT_SORT, direction = 'desc', ...filters} = {}) {
  const sign = direction === 'asc' ? 1 : -1;
  return filterRows(rows, filters)
    .sort((a, b) => {
      const av = sortValue(a, sort), bv = sortValue(b, sort);
      if (av === null && bv !== null) return 1;
      if (bv === null && av !== null) return -1;
      if (av !== bv && av !== null) return sign * (av - bv);
      return (numeric(b.stars) ?? -1) - (numeric(a.stars) ?? -1) || String(a.full_name).localeCompare(String(b.full_name));
    });
}
/* The next sort state after a click on a header: a new column starts descending, the same column flips. */
function nextSort(current, key) {
  if (current.sort === key) return {sort: key, direction: current.direction === 'desc' ? 'asc' : 'desc'};
  return {sort: key, direction: 'desc'};
}
/* [label, value] pairs for a repository record as returned by GET /repos/{owner}/{repo} (already reduced by repoOutcome); anything else is unknown. */
function metadataFields(metadata) {
  const m = metadata && typeof metadata === 'object' && !Array.isArray(metadata) ? metadata : {};
  const count = value => numeric(value) !== null && numeric(value) >= 0 ? displayNumber(value) : UNKNOWN;
  const topics = Array.isArray(m.topics) && m.topics.every(topic => typeof topic === 'string') ? (m.topics.join(', ') || 'None') : UNKNOWN;
  return [
    ['Description', typeof m.description === 'string' ? (m.description.trim() ? m.description : 'None') : UNKNOWN], ['Topics', topics], ['Forks', count(m.forks)],
    ['Created (UTC)', timeLabel(m.created_at)], ['Last pushed (UTC)', timeLabel(m.pushed_at)], ['Archived', typeof m.archived === 'boolean' ? (m.archived ? 'Yes' : 'No') : UNKNOWN]
  ];
}
/* GitHub list endpoints paginate with a Link header; the last page number of a per_page=1 request is the total count. */
function linkCount(linkHeader, bodyLength) {
  const last = typeof linkHeader === 'string' && /[?&]page=(\d+)[^>]*>;\s*rel="last"/.exec(linkHeader);
  if (last) return Number(last[1]);
  return Number.isSafeInteger(bodyLength) && bodyLength >= 0 ? bodyLength : 0;
}
/* value: count | 'too_many' | 'unavailable' | null (undetermined); cache: safe to keep for the session; limit: {reset} when rate limited. */
function countOutcome(status, getHeader, body) {
  const message = body && typeof body === 'object' && !Array.isArray(body) && typeof body.message === 'string' ? body.message : '';
  if (status === 200) return {value: linkCount(getHeader('link'), Array.isArray(body) ? body.length : 0), cache: true, limit: null};
  if (status === 204) return {value: 0, cache: true, limit: null};
  if ((status === 403 || status === 429) && (getHeader('x-ratelimit-remaining') === '0' || /rate limit/i.test(message))) {
    const seconds = Number(getHeader('x-ratelimit-reset'));
    return {value: null, cache: false, limit: {reset: Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : null}};
  }
  if (status === 403 && /too large/i.test(message)) return {value: 'too_many', cache: true, limit: null};
  if (status === 404 || status === 451) return {value: 'unavailable', cache: true, limit: null};
  return {value: 'unavailable', cache: false, limit: null};
}
/* value: the rendered fields of GET /repos/{owner}/{repo} | 'unavailable' | null (rate limited); same cache/limit rules as countOutcome. */
function repoOutcome(status, getHeader, body) {
  if (status === 200 && body && typeof body === 'object' && !Array.isArray(body)) {
    const text = value => typeof value === 'string' ? value : null;
    return {value: {description: text(body.description), topics: Array.isArray(body.topics) && body.topics.every(topic => typeof topic === 'string') ? body.topics : null,
      forks: numeric(body.forks_count), created_at: text(body.created_at), pushed_at: text(body.pushed_at), archived: typeof body.archived === 'boolean' ? body.archived : null}, cache: true, limit: null};
  }
  const outcome = countOutcome(status === 200 ? 500 : status, getHeader, body);
  return {value: outcome.value === null ? null : 'unavailable', cache: outcome.cache, limit: outcome.limit};
}
function countLabel(value) {
  if (value === 'too_many') return 'too many to count';
  if (value === 'unavailable') return 'unavailable';
  return numeric(value) === null ? UNKNOWN : displayNumber(value);
}
function apiRepoPath(name) {
  return repoURL(name) && !name.split('/').some(part => /^\.{1,2}$/.test(part)) ? name : null;
}
/* One expansion = four GitHub requests: the repository record plus the three counts. Definite outcomes are kept in storage for the session. */
function createCountLoader(fetcher, storage, now = () => Date.now()) {
  const read = key => { try { const item = JSON.parse(storage.getItem(key)); return item && typeof item === 'object' && item.fields && typeof item.fields === 'object' ? item : null; } catch (_) { return null; } };
  const requests = [['repo', '', repoOutcome], ...GITHUB_COUNTS.map(([field, , path]) => [field, '/' + path, countOutcome])];
  return async (name, onField = () => {}) => {
    if (!apiRepoPath(name)) throw new Error('Invalid repository name');
    const key = `gh-counts:${name.toLowerCase()}`, cached = read(key) || {fetched_at: null, fields: {}};
    const result = {fields: {}, fetched_at: cached.fetched_at, limit: null, fetched: false};
    const pending = [];
    for (const [field, path, outcomeOf] of requests) {
      if (cached.fields[field] !== undefined) { result.fields[field] = cached.fields[field]; onField(field, cached.fields[field]); continue; }
      pending.push(Promise.resolve().then(() => fetcher(`https://api.github.com/repos/${name}${path}`, {credentials: 'omit', mode: 'cors'})).then(async response => {
        let body = null;
        try { body = await response.json(); } catch (_) { body = null; }
        return outcomeOf(response.status, header => response.headers.get(header), body);
      }, () => ({value: 'unavailable', cache: false, limit: null})).then(outcome => {
        if (outcome.limit) result.limit = outcome.limit;
        result.fields[field] = outcome.value;
        if (outcome.cache) cached.fields[field] = outcome.value;
        onField(field, outcome.value);
      }));
    }
    if (pending.length) {
      await Promise.all(pending);
      result.fetched = true; result.fetched_at = new Date(now()).toISOString();
      if (Object.keys(cached.fields).length) { try { storage.setItem(key, JSON.stringify({fetched_at: result.fetched_at, fields: cached.fields})); } catch (_) {} }
    }
    return result;
  };
}
if (typeof module !== 'undefined') module.exports = {captureNote, pendingTitle, pendingNote, RETIRED_SORTS, numeric, utcDay, parseRange, parseCreated, parseStars, formatRange, createdDay, filterRows, pageOf, parseQuery, serializeQuery, invalidNote, keepEarlyEdits, activeFilters, UNKNOWN_LANGUAGE, NO_FILTERS, PAGE_SIZE, creationTime, creationLabel, repoURL, displayNumber, timeLabel, clockLabel, parseData, windowTooltip, windowNote, sortValue, selectRows, nextSort, metadataFields, linkCount, countOutcome, repoOutcome, countLabel, apiRepoPath, createCountLoader, COLUMNS, GAIN_COLUMNS, GITHUB_COUNTS, DATA_FILE, DATA_SCHEMA, DEFAULT_SORT};
if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  let data = null, page = 1, state = {sort: DEFAULT_SORT, direction: 'desc', ...NO_FILTERS}, linkNote = null, selection = {key: null, rows: []};
  const session = (() => { try { return window.sessionStorage; } catch (_) { return null; } })();
  const memory = new Map(), storage = session || {getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value)};
  const loadCounts = createCountLoader((...args) => fetch(...args), storage);
  /* Drawn icons (24-unit grid, 1.75 stroke, round caps), the Practice Lab's set. */
  const ICONS = {x: 'M6.5 6.5l11 11M17.5 6.5l-11 11', sun: 'M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4',
    moon: 'M20 14.2A8 8 0 0 1 9.8 4a8 8 0 1 0 10.2 10.2z', system: 'M4 5h16v11H4zM9 20h6M12 16v4'};
  const icon = name => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'), path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    for (const [k, v] of Object.entries({class: 'icon', viewBox: '0 0 24 24', width: '16', height: '16', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.75', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true'})) svg.setAttribute(k, v);
    path.setAttribute('d', ICONS[name]); svg.append(path); return svg;
  };
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function openDetails(panel, row) {
    const list = node('dl', undefined, 'repo-metadata'), note = node('p', 'Loading from GitHub…', 'details-note');
    const cells = {};
    for (const [label] of metadataFields(null)) { cells[label] = node('dd', '…'); list.append(node('dt', label), cells[label]); }
    for (const [field, label] of GITHUB_COUNTS) { cells[field] = node('dd', '…'); list.append(node('dt', label), cells[field]); }
    panel.append(list, note);
    const show = (field, value) => {
      if (field !== 'repo') { cells[field].textContent = countLabel(value); return; }
      for (const [label, text] of metadataFields(value === 'unavailable' ? null : value)) cells[label].textContent = text;
    };
    loadCounts(row.full_name, show).then(result => {
      if (result.limit) note.textContent = result.limit.reset ? `GitHub limit reached — resets ${clockLabel(result.limit.reset)}` : 'GitHub limit reached — try again later';
      else note.textContent = `From the GitHub API, fetched ${clockLabel(result.fetched_at)}`;
    }, () => { for (const key in cells) cells[key].textContent = UNKNOWN; note.textContent = 'GitHub details unavailable.'; });
  }
  /* Header buttons carry the sort state; a history-sourced window gets its ≈ marker and tooltip once, when the data arrives, or the
     missed-capture note instead. */
  function renderHeaders() {
    for (const button of document.querySelectorAll('th button.sort')) {
      const key = button.dataset.key, th = button.parentElement, active = key === state.sort;
      th.setAttribute('aria-sort', active ? (state.direction === 'asc' ? 'ascending' : 'descending') : 'none');
      th.classList.toggle('sorted', active);
      const window = data && data.windows.find(w => w.column === key);
      const note = windowNote(window, data && data.date);
      if (note && !button.querySelector('.missed')) {
        const mark = node('span', 'n/a', 'missed'); mark.title = note; mark.setAttribute('aria-hidden', 'true'); button.append(mark, node('span', note, 'sr-only'));
      } else if (!note && window && window.source === 'history' && !button.querySelector('.approx')) {
        const mark = node('span', '≈', 'approx'); mark.title = windowTooltip(window); button.append(mark);
      }
    }
    el('sort').value = state.sort;
    for (const button of document.querySelectorAll('#direction button')) button.setAttribute('aria-pressed', String(button.dataset.dir === state.direction));
  }
  /* The filter controls show the state (after a link, back/forward or a chip removal); the state is read back from them on input. */
  function writeControls() {
    el('search').value = state.search; el('language').value = state.language;
    el('created-after').value = state.created.after || ''; el('created-before').value = state.created.before || '';
    el('stars-min').value = state.stars.min === null ? '' : String(state.stars.min); el('stars-max').value = state.stars.max === null ? '' : String(state.stars.max);
    for (const id of ['stars-min', 'stars-max']) el(id).removeAttribute('aria-invalid');
  }
  function readControls() {
    let invalid = false;
    const star = id => {
      const text = el(id).value.trim(), n = starCount(text), bad = el(id).validity.badInput || (!!text && n === null);
      if (bad) { invalid = true; el(id).setAttribute('aria-invalid', 'true'); } else el(id).removeAttribute('aria-invalid');
      return bad ? null : n;
    };
    const next = {search: el('search').value, language: data ? el('language').value : state.language, created: {after: utcDay(el('created-after').value), before: utcDay(el('created-before').value)},
      stars: {min: star('stars-min'), max: star('stars-max')}};
    return {next, note: invalid ? 'Star bounds are whole numbers; the invalid bound is ignored.' : null};
  }
  function renderChips() {
    const list = el('chips'), active = activeFilters(state);
    list.replaceChildren();
    for (const [key, value] of active) {
      const item = node('li'), chip = node('button', undefined, 'chip'), name = key === 'q' ? 'name' : key;
      chip.type = 'button'; chip.dataset.filter = key; chip.setAttribute('aria-label', `Remove filter ${name}: ${value}`);
      chip.append(node('span', `${name}:`, 'chip-key'), node('span', value, 'chip-value'), icon('x'));
      item.append(chip); list.append(item);
    }
    el('active-filters').hidden = !active.length;
  }
  function renderNotes(inputNote) {
    const text = [linkNote, inputNote].filter(Boolean).join(' ');
    el('filter-note').textContent = text; el('filter-note').hidden = !text;
  }
  function render() {
    if (!data) return;
    const {rows, windows} = data, chosen = GAIN_COLUMNS.includes(state.sort) ? state.sort : DEFAULT_SORT;
    const key = JSON.stringify([state.sort, state.direction, state.search.trim().toLowerCase(), state.language, state.created, state.stars]);
    if (selection.key !== key) selection = {key, rows: selectRows(rows, state)};  /* paging reuses the filtered, sorted list */
    const sorted = selection.rows, current = pageOf(sorted, page); page = current.page;
    const {pages, offset} = current, body = el('rows'); body.replaceChildren();
    const total = rows.length.toLocaleString('en-US'), threshold = data.min_stars === null ? '' : ` with at least ${data.min_stars.toLocaleString('en-US')} stars`;
    el('result-count').textContent = sorted.length === rows.length ? `${total} repositories${threshold}` : `${sorted.length.toLocaleString('en-US')} of ${total} repositories${threshold}`;
    if (data.min_stars !== null) { el('min-stars').textContent = data.min_stars.toLocaleString('en-US'); document.title = `Momentum · Repositories with at least ${data.min_stars.toLocaleString('en-US')} stars`; }
    el('updated').textContent = timeLabel(data.generated_at) !== UNKNOWN ? `Updated ${timeLabel(data.generated_at).slice(0, 16)} UTC` : '';
    renderHeaders(); renderChips();
    const note = windowNote(windows.find(w => w.column === chosen), data.date);  /* cards have no header row to carry it */
    el('sort-note').textContent = note || ''; el('sort-note').hidden = !note;
    const status = captureNote(data); el('capture-status').textContent = status || ''; el('capture-status').hidden = !status;
    const pending = pendingNote(data); el('pending-note').textContent = pending || ''; el('pending-note').hidden = !pending;
    for (const [index, row] of current.rows.entries()) {
      const tr = node('tr'), repo = node('td', undefined, 'repo'), url = repoURL(row.full_name);
      const title = node(url ? 'a' : 'span', row.full_name || UNKNOWN, 'repo-name');
      if (url) { title.href = url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
      repo.append(title);
      const details = node('details', undefined, 'repo-details'), panel = node('div', undefined, 'details-panel');
      details.append(node('summary', 'Repository details'), panel);
      let attempted = false;
      details.addEventListener('toggle', () => { if (details.open && !attempted) { attempted = true; openDetails(panel, row); } });
      repo.append(details);
      const language = node('td', row.language || UNKNOWN, 'lang'), created = node('td', creationLabel(row.created_at), 'created');
      const stars = node('td', displayNumber(row.stars), 'num stars');
      if (!row.language) language.classList.add('muted');
      if (sortValue(row, 'created_at') === null) created.classList.add('muted'); else created.title = `Created ${timeLabel(row.created_at)}`;
      if (numeric(row.stars) === null) stars.classList.add('muted');
      tr.append(node('td', String(offset + index + 1), 'rank'), repo, language, created, stars);
      for (const [index, key] of GAIN_COLUMNS.entries()) {
        const value = numeric(row[key]), cell = node('td', displayNumber(value, true), 'num gain');
        cell.dataset.label = COLUMNS.find(([k]) => k === key)[1];
        if (value === null) { cell.classList.add('muted'); if (row.pending) { cell.classList.add('pending'); cell.title = pendingTitle(row.retrying); cell.setAttribute('aria-describedby', 'pending-note'); } } else if (value > 0) cell.classList.add('gain-positive');
        if (value !== null && (row.history_mask & (1 << index))) { cell.classList.add('approx'); cell.title = windowTooltip(windows[index]); }
        if (key === chosen) cell.classList.add('chosen');
        tr.append(cell);
      }
      body.append(tr);
    }
    if (!sorted.length) { const td = node('td', 'No repositories match these filters. Remove a filter or clear them all.', 'empty'); td.colSpan = 5 + GAIN_COLUMNS.length; const tr = node('tr'); tr.append(td); body.append(tr); }
    el('previous').disabled = page <= 1; el('next').disabled = page >= pages;
    el('page-label').textContent = `Page ${page} of ${pages}`;
  }
  /* Every change lands in the URL: discrete changes push a history entry, typing replaces the current one, back/forward restore it. Writes
     within 120 ms coalesce into one (a burst of clicks is one entry, and the browser's history rate limit is never reached). */
  let urlWrite = null;
  function writeURL(push) {
    urlWrite = {push: push || (urlWrite && urlWrite.push), timer: (clearTimeout(urlWrite && urlWrite.timer), setTimeout(() => {
      const url = `${location.pathname}${serializeQuery({...state, page})}${location.hash}`, mode = urlWrite.push ? 'pushState' : 'replaceState'; urlWrite = null;
      if (url !== `${location.pathname}${location.search}${location.hash}`) history[mode](null, '', url);
    }, 120))};
  }
  function commit(changes, {push = true, keepPage = false} = {}) {
    state = {...state, ...changes}; if (!keepPage) page = 1;
    render(); writeURL(push);
  }
  function setSort(next) { commit(next); }
  function applyFilters(push) { const {next, note} = readControls(); linkNote = null; renderNotes(note); commit(next, {push}); }
  /* State from the URL: invalid values are dropped and named in a note; a language the dataset does not have is dropped the same way. */
  function loadURL(keepEdits = false) {
    const {state: fromURL, invalid} = parseQuery(location.search), edits = keepEdits ? readControls() : null;
    const parsed = edits ? keepEarlyEdits(fromURL, {...state, ...edits.next}) : fromURL;
    if (parsed.language && ![...el('language').options].some(option => option.value === parsed.language)) { invalid.push(['language', parsed.language, QUERY_HELP.language]); parsed.language = ''; }
    page = parsed.page; delete parsed.page; state = parsed; linkNote = invalidNote(invalid);
    writeControls(); renderNotes(edits && edits.note); render();
    if (edits && parsed !== fromURL) writeURL(false);  /* only a real change rewrites the link */
  }
  const debounce = (fn, ms) => { let timer = null; return () => { clearTimeout(timer); timer = setTimeout(fn, ms); }; };
  /* Until the data arrives the form shows the link's state; an edit made meanwhile is kept and applied on load, not replaced by the URL. */
  let editedEarly = false;
  { const {state: fromURL} = parseQuery(location.search); page = fromURL.page; delete fromURL.page; state = fromURL; writeControls(); renderHeaders(); }
  for (const type of ['input', 'change', 'click']) document.addEventListener(type, event => { if (!data && event.target instanceof Element && event.target.closest('.filters, .sort-controls, thead')) editedEarly = true; }, true);
  fetch(DATA_FILE, {cache: 'no-store'}).then(response => {
    if (!response.ok) throw new Error(`Data request failed (${response.status}).`);
    return response.json();
  }).then(payload => {
    data = parseData(payload);
    const counts = new Map();
    for (const row of data.rows) { const language = row.language || UNKNOWN_LANGUAGE; counts.set(language, (counts.get(language) || 0) + 1); }
    const unknown = counts.get(UNKNOWN_LANGUAGE); counts.delete(UNKNOWN_LANGUAGE);
    for (const [language, count] of [...[...counts].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)), ...(unknown ? [[UNKNOWN_LANGUAGE, unknown]] : [])]) {
      const option = node('option', `${language} (${count.toLocaleString('en-US')})`); option.value = language; el('language').append(option);
    }
    loadURL(editedEarly);
  }).catch(error => {
    el('error').hidden = false; el('error').textContent = `${error.message} This page must be served over HTTP(S), not opened as a local file.`;
    el('result-count').textContent = 'No data loaded.'; el('page-label').textContent = '';
  });
  for (const button of document.querySelectorAll('th button.sort')) button.addEventListener('click', () => setSort(nextSort(state, button.dataset.key)));
  el('sort').addEventListener('change', () => setSort({sort: el('sort').value, direction: state.direction}));
  for (const button of document.querySelectorAll('#direction button')) button.addEventListener('click', () => setSort({sort: state.sort, direction: button.dataset.dir}));
  const typed = debounce(() => applyFilters(false), 150);
  for (const id of ['search', 'stars-min', 'stars-max']) { el(id).addEventListener('input', typed); el(id).addEventListener('change', () => applyFilters(true)); }
  for (const id of ['language', 'created-after', 'created-before']) el(id).addEventListener('change', () => applyFilters(true));
  el('chips').addEventListener('click', event => {
    const chip = event.target.closest('button.chip'); if (!chip) return;
    const chips = [...el('chips').querySelectorAll('button.chip')], index = chips.indexOf(chip);
    commit({q: {search: ''}, language: {language: ''}, created: {created: NO_FILTERS.created}, stars: {stars: NO_FILTERS.stars}}[chip.dataset.filter]);
    writeControls(); linkNote = null; renderNotes(null);
    const left = [...el('chips').querySelectorAll('button.chip')];  /* keep keyboard focus in the chip row, else return it to the search box */
    (left[Math.min(index, left.length - 1)] || el('search')).focus();
  });
  el('clear-all').addEventListener('click', () => { commit({...NO_FILTERS}); writeControls(); linkNote = null; renderNotes(null); el('search').focus(); });
  window.addEventListener('popstate', () => { if (urlWrite) { clearTimeout(urlWrite.timer); urlWrite = null; } if (data) loadURL(); });
  for (const [id, delta] of [['previous', -1], ['next', 1]]) el(id).addEventListener('click', () => { page += delta; commit({}, {keepPage: true}); });
  /* Theme: system -> light -> dark -> system, stored like the Practice Lab's and applied before first paint by index.html's inline script. */
  const THEME_KEY = 'sgt:v1:theme', THEMES = {system: ['light', 'System theme', 'system'], light: ['dark', 'Light theme', 'sun'], dark: ['system', 'Dark theme', 'moon']};
  const readTheme = () => { try { const t = JSON.parse(localStorage.getItem(THEME_KEY) || '"system"'); return THEMES[t] ? t : 'system'; } catch (_) { return 'system'; } };
  function applyTheme(theme) {
    if (theme === 'system') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', theme);
    const dark = theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
    document.querySelector('meta[name="theme-color"]').setAttribute('content', dark ? '#0b0b0c' : '#fafafa');
    const [next, label, glyph] = THEMES[theme], button = el('theme');
    button.dataset.theme = theme; button.title = `${label} (click for ${THEMES[next][1].toLowerCase()})`;
    button.querySelector('.theme-label').textContent = label; button.querySelector('svg').replaceWith(icon(glyph));
  }
  el('theme').addEventListener('click', () => { const next = THEMES[readTheme()][0]; try { localStorage.setItem(THEME_KEY, JSON.stringify(next)); } catch (_) {} applyTheme(next); });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => readTheme() === 'system' && applyTheme('system'));
  applyTheme(readTheme());
}
