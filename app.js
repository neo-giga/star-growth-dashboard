/* Static star-growth explorer: every repository above the threshold in one table, sortable by any column; repository details come live from the unauthenticated GitHub API. */
'use strict';
const PAGE_SIZE = 50;
const DATA_FILE = 'data.json';
const DATA_SCHEMA = 'star-growth-dashboard/page/1';
const UNKNOWN = '—';
const DEFAULT_SORT = 'gain_7d';
/* Sortable columns in table order: key, header label, kind (text columns sort ascending on the first click, numbers descending). */
const COLUMNS = [
  ['full_name', 'Repository', 'text'], ['language', 'Language', 'text'], ['created_at', 'Created', 'time'], ['stars', 'Stars', 'number'],
  ['gain_1d', '1 day', 'gain'], ['gain_7d', '1 week', 'gain'], ['gain_15d', '15 days', 'gain'], ['gain_30d', '1 month', 'gain'],
  ['gain_90d', '3 months', 'gain'], ['gain_180d', '6 months', 'gain'], ['gain_365d', '1 year', 'gain']
];
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
   (lower-cased name) precomputed; the creation instant is parsed once per row the first time the created column is sorted. */
function parseData(data) {
  if (!data || typeof data !== 'object' || data.schema !== DATA_SCHEMA || !data.columns || typeof data.columns !== 'object' || !Array.isArray(data.windows)) throw new Error('The local data file has an invalid format.');
  const count = numeric(data.row_count), columns = data.columns;
  const names = ['full_name', 'language', 'created_at', 'stars', ...GAIN_COLUMNS, 'history_mask'];
  if (count === null || count < 0 || names.some(name => !Array.isArray(columns[name]) || columns[name].length !== count)) throw new Error('The local data file has an invalid format.');
  const windows = GAIN_COLUMNS.map(key => data.windows.find(w => w && w.column === key) || {column: key, source: null, exact_since: null});
  const rows = new Array(count);
  for (let i = 0; i < count; i++) {
    const row = {full_name: columns.full_name[i], language: columns.language[i], created_at: columns.created_at[i], stars: columns.stars[i], history_mask: numeric(columns.history_mask[i]) || 0};
    for (const key of GAIN_COLUMNS) row[key] = columns[key][i];
    row.search = String(row.full_name || '').toLowerCase();
    rows[i] = row;
  }
  return {rows, windows, generated_at: data.generated_at, date: data.date, threshold: numeric(data.threshold), row_count: count};
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
/* A row's value for a sort key: null sorts last in both directions. */
function sortValue(row, key) {
  if (key === 'created_at') { if (row.created === undefined) row.created = creationTime(row.created_at); return row.created; }
  if (key === 'full_name' || key === 'language') return typeof row[key] === 'string' && row[key] ? row[key].toLowerCase() : null;
  return numeric(row[key]);
}
function selectRows(rows, {sort = DEFAULT_SORT, direction = 'desc', search = '', language = ''} = {}) {
  const query = search.trim().toLowerCase(), sign = direction === 'asc' ? 1 : -1;
  const kind = (COLUMNS.find(([key]) => key === sort) || [])[2] || 'number';
  return rows.filter(row => (!language || (row.language || UNKNOWN) === language) && (!query || (row.search || String(row.full_name || '').toLowerCase()).includes(query)))
    .sort((a, b) => {
      const av = sortValue(a, sort), bv = sortValue(b, sort);
      if (av === null && bv !== null) return 1;
      if (bv === null && av !== null) return -1;
      if (av !== bv && av !== null) return kind === 'text' ? sign * av.localeCompare(bv) : sign * (av - bv);
      return (numeric(b.stars) ?? -1) - (numeric(a.stars) ?? -1) || String(a.full_name).localeCompare(String(b.full_name));
    });
}
/* The next sort state after a click on a header: a new column starts descending (text columns ascending), the same column flips. */
function nextSort(current, key) {
  const kind = (COLUMNS.find(([k]) => k === key) || [])[2] || 'number';
  if (current.sort === key) return {sort: key, direction: current.direction === 'desc' ? 'asc' : 'desc'};
  return {sort: key, direction: kind === 'text' ? 'asc' : 'desc'};
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
if (typeof module !== 'undefined') module.exports = {numeric, creationTime, creationLabel, repoURL, displayNumber, timeLabel, clockLabel, parseData, windowTooltip, windowNote, sortValue, selectRows, nextSort, metadataFields, linkCount, countOutcome, repoOutcome, countLabel, apiRepoPath, createCountLoader, COLUMNS, GAIN_COLUMNS, GITHUB_COUNTS, DATA_FILE, DATA_SCHEMA, DEFAULT_SORT};
if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  let data = null, page = 1, state = {sort: DEFAULT_SORT, direction: 'desc'};
  const session = (() => { try { return window.sessionStorage; } catch (_) { return null; } })();
  const memory = new Map(), storage = session || {getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value)};
  const loadCounts = createCountLoader((...args) => fetch(...args), storage);
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
    el('sort').value = state.sort; el('direction').value = state.direction;
  }
  function render() {
    if (!data) return;
    const {rows, windows} = data, chosen = GAIN_COLUMNS.includes(state.sort) ? state.sort : DEFAULT_SORT;
    const sorted = selectRows(rows, {...state, search: el('search').value, language: el('language').value});
    const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE)); page = Math.min(page, pages);
    const offset = (page - 1) * PAGE_SIZE, body = el('rows'); body.replaceChildren();
    const total = rows.length.toLocaleString('en-US'), threshold = data.threshold === null ? '' : ` above ${data.threshold.toLocaleString('en-US')} stars`;
    el('result-count').textContent = sorted.length === rows.length ? `${total} repositories${threshold}` : `${sorted.length.toLocaleString('en-US')} of ${total} repositories${threshold}`;
    el('updated').textContent = timeLabel(data.generated_at) !== UNKNOWN ? `Updated ${timeLabel(data.generated_at).slice(0, 16)} UTC` : '';
    renderHeaders();
    for (const [index, row] of sorted.slice(offset, offset + PAGE_SIZE).entries()) {
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
        if (value === null) cell.classList.add('muted'); else if (value > 0) cell.classList.add('gain-positive');
        if (value !== null && (row.history_mask & (1 << index))) { cell.classList.add('approx'); cell.title = windowTooltip(windows[index]); }
        if (key === chosen) cell.classList.add('chosen');
        tr.append(cell);
      }
      body.append(tr);
    }
    if (!sorted.length) { const td = node('td', 'No matches. Try clearing the search or language filter.', 'empty'); td.colSpan = 5 + GAIN_COLUMNS.length; const tr = node('tr'); tr.append(td); body.append(tr); }
    el('previous').disabled = page <= 1; el('next').disabled = page >= pages;
    el('page-label').textContent = `Page ${page} of ${pages}`;
  }
  function setSort(next) { state = next; page = 1; render(); }
  fetch(DATA_FILE, {cache: 'no-store'}).then(response => {
    if (!response.ok) throw new Error(`Data request failed (${response.status}).`);
    return response.json();
  }).then(payload => {
    data = parseData(payload);
    const languages = [...new Set(data.rows.map(r => r.language || UNKNOWN))].sort();
    for (const language of languages) { const option = node('option', language); option.value = language; el('language').append(option); }
    render();
  }).catch(error => {
    el('error').hidden = false; el('error').textContent = `${error.message} This page must be served over HTTP(S), not opened as a local file.`;
    el('result-count').textContent = 'No data loaded.'; el('page-label').textContent = '';
  });
  for (const button of document.querySelectorAll('th button.sort')) button.addEventListener('click', () => setSort(nextSort(state, button.dataset.key)));
  el('sort').addEventListener('change', () => setSort({sort: el('sort').value, direction: state.direction}));
  el('direction').addEventListener('change', () => setSort({sort: state.sort, direction: el('direction').value}));
  for (const id of ['language', 'search']) el(id).addEventListener(id === 'search' ? 'input' : 'change', () => {page = 1; render();});
  for (const [id, delta] of [['previous', -1], ['next', 1]]) el(id).addEventListener('click', () => {page += delta; render();});
}
