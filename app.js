/* Static star-growth explorer. Each window's rows load from their own data file when first selected; repository details come live from the unauthenticated GitHub API. */
'use strict';
const PAGE_SIZE = 50;
const DAYS = {day: 1, week: 7, month: 30};
const DATA_FILES = {day: 'data-day.json', week: 'data-week.json', month: 'data-month.json'};
const UNKNOWN = '—';
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
function selectRows(rows, {sort = 'gain', search = '', language = ''} = {}) {
  const query = search.trim().toLowerCase();
  return rows.filter(row => (!language || (row.language || UNKNOWN) === language) &&
    (!query || `${row.full_name || ''} ${row.description || ''}`.toLowerCase().includes(query)))
    .slice().sort((a, b) => {
      const metric = sort === 'created_at' ? creationTime : numeric;
      const av = metric(a[sort]), bv = metric(b[sort]);
      if (av === null && bv !== null) return 1;
      if (bv === null && av !== null) return -1;
      if (av !== bv) return bv - av;
      return (numeric(b.stars) ?? -1) - (numeric(a.stars) ?? -1) || String(a.full_name).localeCompare(String(b.full_name));
    });
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
/* The feed for one window out of its data file, or an Error. */
function parseFeed(data, period) {
  if (!data || !Array.isArray(data.feeds)) throw new Error('The local data file has an invalid format.');
  const feeds = data.feeds.filter(f => f && f.provider === 'gittrend' && f.period === period && Array.isArray(f.rows));
  if (feeds.length !== 1) throw new Error(`The local data file does not hold exactly one ${period} feed.`);
  return feeds[0];
}
/* A tab opened before a later deploy asks for a data file that is gone (404): reload index.html once per missing file name, never again
   for the same name, so a deploy that is itself broken shows the error instead of looping. Needs real sessionStorage to remember. */
const RELOADED_KEY = 'reloaded-for-missing';
function reloadOnceFor(session, name) {
  if (!session) return false;
  try {
    const tried = JSON.parse(session.getItem(RELOADED_KEY) || '[]');
    if (!Array.isArray(tried) || tried.includes(name)) return false;
    session.setItem(RELOADED_KEY, JSON.stringify([...tried, name].slice(-20)));
    return JSON.parse(session.getItem(RELOADED_KEY)).includes(name);
  } catch (_) { return false; }
}
if (typeof module !== 'undefined') module.exports = {numeric, creationTime, creationLabel, selectRows, repoURL, displayNumber, timeLabel, clockLabel, metadataFields, linkCount, countOutcome, repoOutcome, countLabel, apiRepoPath, createCountLoader, parseFeed, reloadOnceFor, GITHUB_COUNTS, DATA_FILES};
if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  const loaded = {}, pending = {};
  let page = 1;
  const session = (() => { try { return window.sessionStorage; } catch (_) { return null; } })();
  const memory = new Map(), storage = session || {getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value)};
  const loadCounts = createCountLoader((...args) => fetch(...args), storage);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function currentFeed() { return loaded[el('period').value]; }
  function loadPeriod(period) {
    if (!pending[period]) pending[period] = fetch(DATA_FILES[period], {cache: 'no-store'}).then(response => {
      if (response.status === 404 && reloadOnceFor(session, DATA_FILES[period])) { location.reload(); return new Promise(() => {}); }
      if (!response.ok) throw new Error(`Data request failed (${response.status}).`);
      return response.json();
    }).then(data => { loaded[period] = parseFeed(data, period); return loaded[period]; }).catch(error => { delete pending[period]; throw error; });
    return pending[period];
  }
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
  function render() {
    const feed = currentFeed(), rows = feed ? feed.rows : [];
    if (!feed && pending[el('period').value]) return;  // the window is still loading: changePeriod shows Loading and renders when its fetch resolves
    const sorted = selectRows(rows, {sort:el('sort').value, search:el('search').value, language:el('language').value});
    const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE)); page = Math.min(page, pages);
    const offset = (page - 1) * PAGE_SIZE, body = el('rows'); body.replaceChildren();
    el('gain-heading').textContent = `${DAYS[el('period').value]}-day gain`;
    el('updated').textContent = feed && timeLabel(feed.fetched_at_end) !== UNKNOWN ? `Updated ${timeLabel(feed.fetched_at_end).slice(0, 16)} UTC` : '';
    el('result-count').textContent = sorted.length === rows.length ? `${rows.length.toLocaleString('en-US')} repositories` : `${sorted.length.toLocaleString('en-US')} of ${rows.length.toLocaleString('en-US')} repositories`;
    for (const [index, row] of sorted.slice(offset, offset + PAGE_SIZE).entries()) {
      const tr = node('tr'), repo = node('td', undefined, 'repo'), url = repoURL(row.full_name);
      const title = node(url ? 'a' : 'span', row.full_name || UNKNOWN, 'repo-name');
      if (url) { title.href = url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
      repo.append(title); if (row.description) repo.append(node('span', String(row.description), 'description'));
      const details = node('details', undefined, 'repo-details'), panel = node('div', undefined, 'details-panel');
      details.append(node('summary', 'Repository details'), panel);
      let attempted = false;
      details.addEventListener('toggle', () => { if (details.open && !attempted) { attempted = true; openDetails(panel, row); } });
      repo.append(details);
      const language = node('td', row.language || UNKNOWN, 'lang'), created = node('td', creationLabel(row.created_at), 'created');
      const stars = node('td', displayNumber(row.stars), 'num stars'), gain = node('td', displayNumber(row.gain, true), 'num gain');
      if (!row.language) language.classList.add('muted');
      if (creationTime(row.created_at) === null) created.classList.add('muted'); else created.title = `Created ${timeLabel(row.created_at)}`;
      if (numeric(row.stars) === null) stars.classList.add('muted');
      if (numeric(row.gain) === null) gain.classList.add('muted'); else if (numeric(row.gain) > 0) gain.classList.add('gain-positive');
      tr.append(node('td', String(offset + index + 1), 'rank'), repo, language, created, stars, gain); body.append(tr);
    }
    if (!sorted.length) { const td = node('td', feed ? 'No matches. Try clearing the search or language filter.' : 'No data loaded for this window.', 'empty'); td.colSpan = 6; const tr = node('tr'); tr.append(td); body.append(tr); }
    el('previous').disabled = page <= 1; el('next').disabled = page >= pages;
    el('page-label').textContent = `Page ${page} of ${pages}`;
  }
  function changePeriod() {
    page = 1; el('error').hidden = true; const period = el('period').value, feed = currentFeed();  // a banner belongs to the window whose load raised it
    if (!feed) {
      el('rows').replaceChildren(); el('result-count').textContent = 'Loading…'; el('page-label').textContent = '';
      loadPeriod(period).then(() => { if (el('period').value === period) changePeriod(); }, error => {
        if (el('period').value !== period) return;
        el('error').hidden = false; el('error').textContent = `${error.message} This page must be served over HTTP(S), not opened as a local file.`; render();
      });
      return;
    }
    const languages = [...new Set(feed.rows.map(r => r.language || UNKNOWN))].sort();
    el('language').replaceChildren(); const all = node('option', 'All languages'); all.value = ''; el('language').append(all);
    for (const language of languages) { const option = node('option', language); option.value = language; el('language').append(option); }
    render();
  }
  el('period').addEventListener('change', changePeriod);
  for (const id of ['sort', 'language', 'search']) el(id).addEventListener(id === 'search' ? 'input' : 'change', () => {page = 1; render();});
  for (const [id, delta] of [['previous', -1], ['next', 1]]) el(id).addEventListener('click', () => {page += delta; render();});
  changePeriod();
}
