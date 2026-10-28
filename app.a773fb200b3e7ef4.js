/* Static GitTrend explorer. Repository details come from the cached snapshot plus live, unauthenticated GitHub API counts. */
'use strict';
const PAGE_SIZE = 50;
const DAYS = {day: 1, week: 7, month: 30};
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
function metadataFields(metadata, createdAt) {
  const m = metadata && typeof metadata === 'object' && !Array.isArray(metadata) ? metadata : {};
  const count = value => numeric(value) !== null && numeric(value) >= 0 ? displayNumber(value) : UNKNOWN;
  const topics = Array.isArray(m.topics) && m.topics.every(topic => typeof topic === 'string') ? (m.topics.join(', ') || 'None') : UNKNOWN;
  return [
    ['Description', typeof m.description === 'string' ? (m.description.trim() ? m.description : 'None') : UNKNOWN], ['Topics', topics], ['Forks', count(m.forks)],
    ['Created (UTC)', timeLabel(createdAt)], ['Last pushed (UTC)', timeLabel(m.pushed_at)], ['Archived', typeof m.archived === 'boolean' ? (m.archived ? 'Yes' : 'No') : UNKNOWN]
  ];
}
function safeMetadataURL(value, base) {
  if (typeof value !== 'string' || !value) throw new Error('Missing metadata URL');
  const url = new URL(value, base), origin = new URL(base).origin;
  if (url.origin !== origin || !['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || !/\/api\/v1\/[A-Za-z0-9_./-]+\.json$/.test(url.pathname)) throw new Error('Unsafe metadata URL');
  return url.href;
}
function metadataFromRecord(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) throw new Error('Invalid metadata record');
  const raw = record.metadata && typeof record.metadata === 'object' && !Array.isArray(record.metadata) ? record.metadata : {};
  const m = {}, p = record.provenance && typeof record.provenance === 'object' && !Array.isArray(record.provenance) ? record.provenance : {};
  for (const key of ['description','pushed_at']) m[key] = typeof raw[key] === 'string' ? raw[key] : null;
  for (const key of ['forks','contributor_count','issue_comment_count','pr_review_comment_count']) m[key] = typeof raw[key] === 'number' && Number.isSafeInteger(raw[key]) && raw[key] >= 0 ? raw[key] : null;
  m.topics = Array.isArray(raw.topics) && raw.topics.every(x => typeof x === 'string') ? raw.topics : null;
  m.archived = typeof raw.archived === 'boolean' ? raw.archived : null;
  m.source = typeof p.metadata_source === 'string' ? p.metadata_source : null;
  m.observed_at = typeof p.metadata_observed_at === 'string' ? p.metadata_observed_at : null;
  m.contributor_measured_at = typeof p.contributor_measured_at === 'string' ? p.contributor_measured_at : null;
  const flags = [p.unknown_flags, p.unknown_fields].filter(Array.isArray);
  m.unknown_flags = flags.length && flags.every(values => values.every(x => typeof x === 'string')) ? [...new Set(flags.flat())] : null;
  return m;
}
function createMetadataLoader(fetcher, base) {
  const pages = new Map();
  return async (row, period) => {
    if (!Number.isSafeInteger(row.repo_id) || row.repo_id < 0 || row.metadata_record_id !== `gittrend:${row.repo_id}`) throw new Error('Missing metadata identity');
    const url = safeMetadataURL(row.metadata_url, base);
    const location = /\/api\/v1\/snapshots\/([a-f0-9]{64})\/(day|week|month)\/pages\/\d{6}\.json$/.exec(new URL(url).pathname);
    if (!location || location[2] !== period) throw new Error('Metadata URL period or snapshot mismatch');
    if (!pages.has(url)) pages.set(url, Promise.resolve().then(() => fetcher(url, {credentials:'omit', mode:'same-origin', redirect:'error'})).then(async response => {
      if (!response.ok) throw new Error('Metadata request failed');
      const page = await response.json();
      if (!page || !Array.isArray(page.rows)) throw new Error('Invalid metadata page');
      if (page.snapshot_id !== location[1] || page.period !== location[2]) throw new Error('Metadata page snapshot or period mismatch');
      return page.rows;
    }));
    const records = await pages.get(url), matching = records.filter(record => record && typeof record === 'object' && record.record_id === row.metadata_record_id);
    if (matching.length !== 1) throw new Error('Metadata identity missing or ambiguous');
    const identity = matching[0].identity;
    if (!identity || identity.provider !== 'gittrend' || identity.provider_repo_id !== row.repo_id || typeof identity.full_name !== 'string' || typeof row.full_name !== 'string' || identity.full_name.toLowerCase() !== row.full_name.toLowerCase()) throw new Error('Metadata repository identity mismatch');
    return metadataFromRecord(matching[0]);
  };
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
function countLabel(value) {
  if (value === 'too_many') return 'too many to count';
  if (value === 'unavailable') return 'unavailable';
  return numeric(value) === null ? UNKNOWN : displayNumber(value);
}
function apiRepoPath(name) {
  return repoURL(name) && !name.split('/').some(part => /^\.{1,2}$/.test(part)) ? name : null;
}
function createCountLoader(fetcher, storage, now = () => Date.now()) {
  const read = key => { try { const item = JSON.parse(storage.getItem(key)); return item && typeof item === 'object' && item.fields && typeof item.fields === 'object' ? item : null; } catch (_) { return null; } };
  return async (name, onField = () => {}) => {
    if (!apiRepoPath(name)) throw new Error('Invalid repository name');
    const key = `gh-counts:${name.toLowerCase()}`, cached = read(key) || {fetched_at: null, fields: {}};
    const result = {fields: {}, fetched_at: cached.fetched_at, limit: null, fetched: false};
    const pending = [];
    for (const [field, , path] of GITHUB_COUNTS) {
      if (cached.fields[field] !== undefined) { result.fields[field] = cached.fields[field]; onField(field, cached.fields[field]); continue; }
      pending.push(Promise.resolve().then(() => fetcher(`https://api.github.com/repos/${name}/${path}`, {credentials: 'omit', mode: 'cors'})).then(async response => {
        let body = null;
        try { body = await response.json(); } catch (_) { body = null; }
        return countOutcome(response.status, header => response.headers.get(header), body);
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
if (typeof module !== 'undefined') module.exports = {numeric, creationTime, creationLabel, selectRows, repoURL, displayNumber, timeLabel, clockLabel, metadataFields, safeMetadataURL, metadataFromRecord, createMetadataLoader, linkCount, countOutcome, countLabel, apiRepoPath, createCountLoader, GITHUB_COUNTS};
if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  let feeds = [], page = 1;
  const loadMetadata = createMetadataLoader((...args) => fetch(...args), document.baseURI);
  const memory = new Map(), storage = (() => { try { return window.sessionStorage; } catch (_) { return null; } })() || {getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value)};
  const loadCounts = createCountLoader((...args) => fetch(...args), storage);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function currentFeed() { return feeds.find(f => f.period === el('period').value); }
  function openDetails(panel, row, period) {
    const cached = node('dl', undefined, 'repo-metadata'), cachedNote = node('p', 'Loading cached details…', 'details-note');
    const live = node('dl', undefined, 'repo-metadata'), liveNote = node('p', 'Counting on GitHub…', 'details-note');
    const cells = {};
    for (const [field, label] of GITHUB_COUNTS) { cells[field] = node('dd', '…'); live.append(node('dt', label), cells[field]); }
    panel.append(cached, cachedNote, live, liveNote);
    const showCached = metadata => { cached.replaceChildren(); for (const [label, value] of metadataFields(metadata, row.created_at)) cached.append(node('dt', label), node('dd', value)); };
    const source = row.metadata && typeof row.metadata === 'object' && !Array.isArray(row.metadata) ? Promise.resolve(row.metadata) : loadMetadata(row, period);
    source.then(metadata => { showCached(metadata); cachedNote.textContent = metadata.observed_at ? `Cached from GitHub API, observed ${timeLabel(metadata.observed_at)}` : 'Cached from GitHub API'; },
      () => { showCached(null); cachedNote.textContent = 'Cached details unavailable.'; });
    loadCounts(row.full_name, (field, value) => { cells[field].textContent = countLabel(value); }).then(result => {
      if (result.limit) liveNote.textContent = result.limit.reset ? `GitHub limit reached — resets ${clockLabel(result.limit.reset)}` : 'GitHub limit reached — try again later';
      else liveNote.textContent = `Counts from GitHub API, fetched ${clockLabel(result.fetched_at)}`;
    }, () => { for (const field in cells) cells[field].textContent = UNKNOWN; liveNote.textContent = 'GitHub counts unavailable.'; });
  }
  function render() {
    const feed = currentFeed(), rows = feed ? feed.rows : [];
    const sorted = selectRows(rows, {sort:el('sort').value, search:el('search').value, language:el('language').value});
    const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE)); page = Math.min(page, pages);
    const offset = (page - 1) * PAGE_SIZE, body = el('rows'); body.replaceChildren();
    el('gain-heading').textContent = `${DAYS[el('period').value]}-day gain`;
    el('updated').textContent = feed && timeLabel(feed.fetched_at_end) !== UNKNOWN ? `Updated ${timeLabel(feed.fetched_at_end).slice(0, 16)} UTC · source: GitTrend` : '';
    el('result-count').textContent = sorted.length === rows.length ? `${rows.length.toLocaleString('en-US')} repositories` : `${sorted.length.toLocaleString('en-US')} of ${rows.length.toLocaleString('en-US')} repositories`;
    for (const [index, row] of sorted.slice(offset, offset + PAGE_SIZE).entries()) {
      const tr = node('tr'), repo = node('td', undefined, 'repo'), url = repoURL(row.full_name);
      const title = node(url ? 'a' : 'span', row.full_name || UNKNOWN, 'repo-name');
      if (url) { title.href = url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
      repo.append(title); if (row.description) repo.append(node('span', String(row.description), 'description'));
      const details = node('details', undefined, 'repo-details'), panel = node('div', undefined, 'details-panel');
      details.append(node('summary', 'Repository details'), panel);
      let attempted = false;
      details.addEventListener('toggle', () => { if (details.open && !attempted) { attempted = true; openDetails(panel, row, feed.period); } });
      repo.append(details);
      const language = node('td', row.language || UNKNOWN, 'lang'), created = node('td', creationLabel(row.created_at), 'created');
      const stars = node('td', displayNumber(row.stars), 'num stars'), gain = node('td', displayNumber(row.gain, true), 'num gain');
      if (!row.language) language.classList.add('muted');
      if (creationTime(row.created_at) === null) created.classList.add('muted'); else created.title = `Created ${timeLabel(row.created_at)}`;
      if (numeric(row.stars) === null) stars.classList.add('muted');
      if (numeric(row.gain) === null) gain.classList.add('muted'); else if (numeric(row.gain) > 0) gain.classList.add('gain-positive');
      tr.append(node('td', String(offset + index + 1), 'rank'), repo, language, created, stars, gain); body.append(tr);
    }
    if (!sorted.length) { const td = node('td', feed ? 'No matches. Try clearing the search or language filter.' : 'No data loaded for this feed.', 'empty'); td.colSpan = 6; const tr = node('tr'); tr.append(td); body.append(tr); }
    el('previous').disabled = page <= 1; el('next').disabled = page >= pages;
    el('page-label').textContent = `Page ${page} of ${pages}`;
  }
  function changePeriod() {
    page = 1; const feed = currentFeed(); const languages = [...new Set((feed?.rows || []).map(r => r.language || UNKNOWN))].sort();
    el('language').replaceChildren(); const all = node('option', 'All languages'); all.value = ''; el('language').append(all);
    for (const language of languages) { const option = node('option', language); option.value = language; el('language').append(option); }
    render();
  }
  el('period').addEventListener('change', changePeriod);
  for (const id of ['sort', 'language', 'search']) el(id).addEventListener(id === 'search' ? 'input' : 'change', () => {page = 1; render();});
  for (const [id, delta] of [['previous', -1], ['next', 1]]) el(id).addEventListener('click', () => {page += delta; render();});
  fetch('data.7f31053344a74c7f.json', {cache:'no-store'}).then(response => {
    if (!response.ok) throw new Error(`Data request failed (${response.status}).`);
    return response.json();
  }).then(data => {
    if (!data || !Array.isArray(data.feeds)) throw new Error('The local data file has an invalid format.');
    feeds = data.feeds.filter(f => f.provider === 'gittrend' && DAYS[f.period] && Array.isArray(f.rows));
    if (new Set(feeds.map(f => f.period)).size !== feeds.length) throw new Error('The data file has duplicate feed periods.');
    changePeriod();
  }).catch(error => {
    el('error').hidden = false; el('error').textContent = `${error.message} This page must be served over HTTP(S), not opened as a local file.`;
    feeds = []; changePeriod();
  });
}
