/* Static, source-separated GitTrend explorer. No remote API calls or analytics. */
'use strict';
const PAGE_SIZE = 50;
const DAYS = {day: 1, week: 7, month: 30};
function numeric(value) {
  if (typeof value === 'string' && /^[+-]?\d+$/.test(value)) value = Number(value);
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
}
function selectRows(rows, {sort = 'gain', search = '', language = ''} = {}) {
  const query = search.trim().toLowerCase();
  return rows.filter(row => (!language || (row.language || 'UNKNOWN') === language) &&
    (!query || `${row.full_name || ''} ${row.description || ''}`.toLowerCase().includes(query)))
    .slice().sort((a, b) => {
      const av = numeric(a[sort]), bv = numeric(b[sort]);
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
if (typeof module !== 'undefined') module.exports = {numeric, selectRows, repoURL, displayNumber, timeLabel};
if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  let feeds = [], page = 1;
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
      ['Retrieval ended', `${timeLabel(fetched)}${age === null ? '' : ` · ${age}h ago`}`]
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
      const stars = node('td', displayNumber(row.stars), 'number'), gain = node('td', displayNumber(row.gain, true), 'number');
      if (numeric(row.stars) === null) stars.classList.add('unknown');
      if (numeric(row.gain) === null) gain.classList.add('unknown'); else if (numeric(row.gain) > 0) gain.classList.add('positive');
      tr.append(node('td', String(offset + index + 1)), name, node('td', row.language || 'UNKNOWN'), stars, gain); body.append(tr);
    }
    if (!sorted.length) { const td = node('td', feed ? 'No matches. Try clearing the search or language filter.' : 'No data loaded for this feed.'); td.colSpan = 5; const tr = node('tr'); tr.append(td); body.append(tr); }
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
  fetch('data.json', {cache:'no-store'}).then(response => {
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
