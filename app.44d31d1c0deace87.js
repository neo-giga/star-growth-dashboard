/* Static, source-separated GitTrend explorer. No remote API calls or analytics. */
'use strict';
const PAGE_SIZE = 50;
const DAYS = {day: 1, week: 7, month: 30};
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
  return instant === null ? 'UNKNOWN' : new Date(instant).toISOString().slice(0, 10);
}
function selectRows(rows, {sort = 'gain', search = '', language = ''} = {}) {
  const query = search.trim().toLowerCase();
  return rows.filter(row => (!language || (row.language || 'UNKNOWN') === language) &&
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
  return n === null ? 'UNKNOWN' : `${gain && n > 0 ? '+' : ''}${n.toLocaleString('en-US')}`;
}
function timeLabel(value) {
  if (!value || Number.isNaN(Date.parse(value))) return 'UNKNOWN';
  return new Date(value).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
}
function metadataFields(metadata) {
  const m = metadata && typeof metadata === 'object' && !Array.isArray(metadata) ? metadata : {};
  const text = value => typeof value === 'string' && value.trim() ? value : 'UNKNOWN';
  const count = value => numeric(value) !== null && numeric(value) >= 0 ? displayNumber(value) : 'UNKNOWN';
  const topics = Array.isArray(m.topics) && m.topics.every(topic => typeof topic === 'string') ? (m.topics.join(', ') || 'None recorded') : 'UNKNOWN';
  return [
    ['Description', typeof m.description === 'string' ? (m.description.trim() ? m.description : 'None recorded') : 'UNKNOWN'], ['Topics', topics], ['Forks', count(m.forks)],
    ['Contributors', count(m.contributor_count)],
    ['Issue comments', count(m.issue_comment_count)], ['PR review comments', count(m.pr_review_comment_count)],
    ['Last pushed (UTC)', timeLabel(m.pushed_at)], ['Archived', typeof m.archived === 'boolean' ? (m.archived ? 'Yes' : 'No') : 'UNKNOWN'],
    ['Metadata source', text(m.source)], ['Metadata observed (UTC)', timeLabel(m.observed_at)], ['Contributors measured (UTC)', timeLabel(m.contributor_measured_at)],
    ['Unknown / quality flags', Array.isArray(m.unknown_flags) && m.unknown_flags.every(flag => typeof flag === 'string') ? (m.unknown_flags.join('; ') || 'None supplied — not a completeness guarantee') : 'UNKNOWN']
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
if (typeof module !== 'undefined') module.exports = {numeric, creationTime, creationLabel, selectRows, repoURL, displayNumber, timeLabel, metadataFields, safeMetadataURL, metadataFromRecord, createMetadataLoader};
if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  let feeds = [], page = 1;
  const loadMetadata = createMetadataLoader((...args) => fetch(...args), document.baseURI);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function currentFeed() { return feeds.find(f => f.period === el('period').value); }
  function metadata(feed) {
    const root = el('metadata'); root.replaceChildren(); el('warnings').replaceChildren();
    if (!feed) { root.textContent = 'No observations loaded for this period. This does not mean zero repositories.'; return; }
    const coverage = feed.coverage || {}, grid = node('div', undefined, 'metadata-grid');
    const fetched = feed.fetched_at_end, age = fetched && !Number.isNaN(Date.parse(fetched)) ? Math.max(0, Math.floor((Date.now() - Date.parse(fetched)) / 3600000)) : null;
    const fields = [
      ['Provider', 'GitTrend · provider-reported gains'],
      ['Coverage status', String(coverage.status || 'UNKNOWN').toUpperCase()],
      ['Observed / provider reported', `${displayNumber(feed.rows.length)} / ${displayNumber(coverage.last_reported_total ?? coverage.reported_total)} (latest)`],
      ['Source snapshot date', feed.source_as_of ? timeLabel(feed.source_as_of) : 'UNKNOWN · not supplied by provider'],
      ['Retrieval began', timeLabel(feed.fetched_at_start)],
      ['Retrieval ended', `${timeLabel(fetched)}${age === null ? '' : ` · ${age}h ago`}`],
      ['GitHub creation dates', `${feed.rows.filter(row => creationTime(row.created_at) !== null).length.toLocaleString()} known · ${feed.rows.filter(row => creationTime(row.created_at) === null).length.toLocaleString()} UNKNOWN`]
    ];
    for (const [label, value] of fields) { const item = node('div'); item.append(node('span', label, 'meta-label'), node('span', value, 'meta-value')); grid.append(item); }
    root.append(grid);
    const warnings = ['This feed is not exhaustive GitHub coverage. Fetch time is not snapshot time.'];
    if (coverage.status !== 'complete') warnings.push('PARTIAL / UNVERIFIED COVERAGE: results are rankings of observed rows only, not verified global top repositories.');
    if (!feed.source_as_of) warnings.push('Source freshness is UNKNOWN; a recent download does not prove recent source data.');
    for (const warning of coverage.warnings || []) warnings.push(typeof warning === 'string' ? warning : JSON.stringify(warning));
    const uniqueWarnings = [...new Set(warnings)];
    for (const warning of uniqueWarnings.slice(0, 5)) el('warnings').append(node('li', warning));
    if (uniqueWarnings.length > 5) {
      const item = node('li'), details = node('details'), list = node('ul');
      details.append(node('summary', `${uniqueWarnings.length - 5} additional validation warnings (expand)`));
      for (const warning of uniqueWarnings.slice(5)) list.append(node('li', warning));
      details.append(list); item.append(details); el('warnings').append(item);
    }
  }
  function render() {
    const feed = currentFeed(), rows = feed ? feed.rows : [];
    const sorted = selectRows(rows, {sort:el('sort').value, search:el('search').value, language:el('language').value});
    const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE)); page = Math.min(page, pages);
    const offset = (page - 1) * PAGE_SIZE, body = el('rows'); body.replaceChildren();
    el('gain-heading').textContent = `${DAYS[el('period').value]}-day gain`;
    el('result-count').textContent = `${sorted.length.toLocaleString()} matches · ${rows.length.toLocaleString()} observed · ${rows.filter(r => numeric(r.gain) === null).length.toLocaleString()} unknown gains`;
    for (const [index, row] of sorted.slice(offset, offset + PAGE_SIZE).entries()) {
      const tr = node('tr'), name = node('td'), url = repoURL(row.full_name);
      const title = node(url ? 'a' : 'span', row.full_name || 'UNKNOWN', 'repo-name');
      if (url) { title.href = url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
      name.append(title); if (row.description) name.append(node('span', String(row.description), 'description'));
      const details = node('details', undefined, 'repo-details'), fields = node('dl', undefined, 'repo-metadata'), status = node('p', undefined, 'details-note');
      details.append(node('summary', 'Cached repository details'));
      const showMetadata = metadata => { fields.replaceChildren(); for (const [label, value] of metadataFields(metadata)) fields.append(node('dt', label), node('dd', value)); };
      let attempted = false;
      if (row.metadata && typeof row.metadata === 'object' && !Array.isArray(row.metadata)) { showMetadata(row.metadata); attempted = true; }
      else if (!row.metadata_url) { showMetadata(null); attempted = true; }
      details.addEventListener('toggle', async () => {
        if (!details.open || attempted) return;
        attempted = true; status.textContent = 'Loading cached metadata…';
        try { showMetadata(await loadMetadata(row, feed.period)); status.textContent = ''; }
        catch (_) { showMetadata(null); status.textContent = 'Metadata unavailable. Values remain UNKNOWN; reload the page to try again.'; }
      });
      details.append(status, fields, node('p', 'Issue comments and PR review comments are separate comment types, not commits. Missing or unmeasured values remain UNKNOWN. Metadata is cached and may be older than the star feed.', 'details-note'));
      name.append(details);
      const stars = node('td', displayNumber(row.stars), 'number'), gain = node('td', displayNumber(row.gain, true), 'number');
      const created = node('td', creationLabel(row.created_at), 'created-date');
      if (creationTime(row.created_at) === null) created.classList.add('unknown');
      else created.title = `GitHub repository created: ${timeLabel(row.created_at)}`;
      if (numeric(row.stars) === null) stars.classList.add('unknown');
      if (numeric(row.gain) === null) gain.classList.add('unknown'); else if (numeric(row.gain) > 0) gain.classList.add('positive');
      tr.append(node('td', String(offset + index + 1)), name, node('td', row.language || 'UNKNOWN'), created, stars, gain); body.append(tr);
    }
    if (!sorted.length) { const td = node('td', feed ? 'No matches. Try clearing the search or language filter.' : 'No data loaded for this feed.'); td.colSpan = 6; const tr = node('tr'); tr.append(td); body.append(tr); }
    el('previous').disabled = page <= 1; el('next').disabled = page >= pages;
    el('page-label').textContent = `Page ${page} of ${pages}`;
  }
  function changePeriod() {
    page = 1; const feed = currentFeed(); const languages = [...new Set((feed?.rows || []).map(r => r.language || 'UNKNOWN'))].sort();
    el('language').replaceChildren(); const all = node('option', 'All languages'); all.value = ''; el('language').append(all);
    for (const language of languages) { const option = node('option', language); option.value = language; el('language').append(option); }
    metadata(feed); render();
  }
  el('period').addEventListener('change', changePeriod);
  for (const id of ['sort', 'language', 'search']) el(id).addEventListener(id === 'search' ? 'input' : 'change', () => {page = 1; render();});
  for (const [id, delta] of [['previous', -1], ['next', 1]]) el(id).addEventListener('click', () => {page += delta; render();});
  fetch('data.16c671ad55bc3628.json', {cache:'no-store'}).then(response => {
    if (!response.ok) throw new Error(`Data request failed (${response.status}).`);
    return response.json();
  }).then(data => {
    if (!data || !Array.isArray(data.feeds)) throw new Error('The local data file has an invalid format.');
    feeds = data.feeds.filter(f => f.provider === 'gittrend' && DAYS[f.period] && Array.isArray(f.rows));
    if (new Set(feeds.map(f => f.period)).size !== feeds.length) throw new Error('The data file has duplicate feed periods.');
    changePeriod();
  }).catch(error => {
    el('error').hidden = false; el('error').textContent = `${error.message} No results are being presented as complete. This static page must be served over HTTP(S), not opened as a local file.`;
    feeds = []; changePeriod();
  });
}
