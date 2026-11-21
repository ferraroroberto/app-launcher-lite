/* The History sheet: what the projects shipped.
 *
 * Opened from the History button in the Projects card. It reads GET
 * /api/progress, which answers from its cache and runs no git or GitLab call,
 * so the sheet draws the cached figures the moment it opens, however old, and
 * says how old each source is. Only the Refresh button asks the server to
 * re-read (?refresh=true); while it runs a toast rides the sheet's footer and
 * the sheet polls the cache, then updates in place. Changing the bucket or the
 * range re-reads the cache only.
 *
 * A figure that was not measured shows its word (unknown, unavailable, stale,
 * skipped), never zero. The maths is progress-model.js; the drawing of a chart
 * is progress-charts.js.
 */

import { AuthRequiredError, jsonApi, logPollFailure, toast } from '../api.js';
import { staleMessage, setAsyncState } from '../async-state.js';
import { el } from '../dom-utils.js';
import { icon } from '../_vendored/icons/icons.js';
import { chip } from '../glance.js';
import { drawChart } from './progress-charts.js';
import {
  DEFAULT_RANGE, cellWord, fmtN, progressUrl, rangeDays, rangeText, repoRows, reposCaption,
  series, summary, tiles,
} from './progress-model.js';

// How often the cache is re-read while a refresh runs, and how many times
// before the sheet stops waiting (a first backfill can take minutes).
const POLL_MS = 4000;
const MAX_POLLS = 150;
// The refresh toast stays until the sheet hides it.
const TOAST_HOLD_MS = 10 * 60 * 1000;

const view = {
  bucket: 'day',
  range: DEFAULT_RANGE,
  custom: { from: '', to: '' },
  sort: 'mrs_merged',
  data: null,
  fetchedAt: null,
  seq: 0,
  failed: false,
  timer: null,
  refreshing: false,
};
// Redraw callbacks for the charts, run again on every resize of their host.
const charts = new Map();
let resizeObserver = null;

function dialog() { return document.getElementById('progressSheet'); }
function node(id) { return document.getElementById(id); }
function isOpen() { const d = dialog(); return !!(d && d.open); }

// ------------------------------------------------------------- reading

function currentRange() {
  return rangeDays(view.range, view.custom, new Date());
}

async function read(refresh) {
  const range = currentRange();
  return jsonApi(progressUrl(view.bucket, range.from, range.to, refresh));
}

function stopPolling() {
  clearTimeout(view.timer);
  view.timer = null;
}

function hideToast() {
  const host = node('progressToast');
  if (host) host.hidden = true;
}

function showToast(message, kind) {
  const host = node('progressToast');
  if (!host) return;
  toast(message, kind || '', { host: host, icon: kind === 'error' ? 'triangle-alert' : 'refresh-cw', ms: TOAST_HOLD_MS });
}

// One read of the cache, drawn. A failed read leaves the last good figures up
// under the stale line; with nothing yet it is the error block with a Retry.
async function load(refresh) {
  const seq = ++view.seq;
  const host = node('progressState');
  if (!view.data) setAsyncState(host, 'loading', { message: 'Reading the history…', glyph: 'hourglass' });
  try {
    const data = await read(refresh);
    if (seq !== view.seq || !isOpen()) return null;
    view.data = data;
    view.fetchedAt = new Date();
    view.failed = false;
    setAsyncState(host, 'ready');
    render();
    return data;
  } catch (exc) {
    if (seq !== view.seq) return null;
    if (exc instanceof AuthRequiredError) return null;
    logPollFailure('progress read failed', exc);
    view.failed = true;
    if (view.data) setAsyncState(host, 'stale', { message: staleMessage(view.fetchedAt) });
    else {
      setAsyncState(host, 'error', {
        message: 'Could not read the history.',
        retry: function () { load(false); },
      });
      node('progressContent').hidden = true;
    }
    return null;
  }
}

// Ask the server to recalculate, then follow the cache until it says it is
// done. The answer to the ask is still the cache, so nothing waits on it.
async function refresh() {
  if (view.refreshing) return;
  view.refreshing = true;
  syncRefreshButton();
  showToast('Refreshing git and GitLab totals…');
  stopPolling();
  await load(true);
  // A read the user's own change superseded still updates view.data, so the
  // loop follows the sheet's latest answer, not one call's return value.
  for (let n = 0; view.data && view.data.refreshing && !view.failed && n < MAX_POLLS && isOpen(); n += 1) {
    await new Promise(function (resolve) { view.timer = setTimeout(resolve, POLL_MS); });
    if (!isOpen()) break;
    await load(false);
  }
  view.refreshing = false;
  syncRefreshButton();
  if (!isOpen()) return;
  if (view.failed || !view.data) showToast('Could not refresh. Showing the last figures.', 'error');
  else if (view.data.refreshing) showToast('Still refreshing. The figures update when it finishes.');
  else hideToast();
}

function syncRefreshButton() {
  const button = node('progressRefresh');
  if (button) button.disabled = view.refreshing;
}

// -------------------------------------------------------------- render

function glyph(name) {
  const span = el('span', 'progress-glyph');
  span.innerHTML = icon(name);
  return span;
}

// The word a missing figure carries: its source's own state word.
function missingWord(status) {
  return status.state === 'unavailable' ? 'unavailable' : 'unknown';
}

function signed(prefix, n) {
  return prefix + fmtN(n);
}

function figure(value, className, prefix) {
  const span = el('span', className, value == null ? '' : (prefix || '') + fmtN(value));
  return span;
}

function tileValue(tile, status) {
  const wrap = el('span', 'progress-tile-value');
  const known = tile.pair.filter(function (v) { return v != null; });
  if (!known.length) {
    const word = el('span', 'progress-missing', missingWord(status));
    wrap.appendChild(word);
    return wrap;
  }
  if (tile.key === 'lines') {
    wrap.append(
      tile.pair[0] == null ? el('span', 'progress-missing', 'unknown') : el('span', 'progress-pos', signed('+', tile.pair[0])),
      ' ',
      tile.pair[1] == null ? el('span', 'progress-missing', 'unknown') : el('span', 'progress-neg', signed('−', tile.pair[1])),
    );
    return wrap;
  }
  tile.pair.forEach(function (v, i) {
    if (i) wrap.appendChild(el('span', 'progress-sep', '/'));
    wrap.appendChild(v == null ? el('span', 'progress-missing', missingWord(status)) : figure(v));
  });
  return wrap;
}

function tileCaption(tile, data) {
  const pair = tile.pair;
  if (tile.key === 'lines' && pair[0] != null && pair[1] != null) {
    const net = pair[0] - pair[1];
    return 'net ' + (net < 0 ? '−' : '') + fmtN(Math.abs(net));
  }
  if (tile.key === 'mrs' && pair[0] != null) {
    const range = currentRange();
    const days = Math.max(1, Math.round((Date.parse(range.to) - Date.parse(range.from)) / 864e5) + 1);
    return (pair[0] / days).toFixed(1) + ' a day';
  }
  if (tile.key === 'issues') return 'opened / closed';
  return '';
}

function renderTiles(data) {
  const sum = summary(data);
  const host = node('progressTiles');
  host.replaceChildren();
  tiles(data).forEach(function (tile) {
    const status = sum[tile.source];
    const card = el('div', 'progress-tile');
    card.dataset.tile = tile.key;
    const label = el('div', 'progress-tile-label');
    label.append(glyph(tile.glyph), tile.label);
    card.append(label, tileValue(tile, status));
    const sub = el('div', 'progress-tile-sub');
    const caption = tileCaption(tile, data);
    if (caption) sub.appendChild(el('span', null, caption));
    if (status.state === 'stale') sub.appendChild(chip('stale', 'neutral'));
    card.appendChild(sub);
    host.appendChild(card);
  });
}

function setNote(id, status) {
  const note = node(id);
  if (note) note.textContent = status.state === 'ok' ? '' : status.word;
}

function paintChart(id, status, draw, subject) {
  const host = node(id);
  if (!host) return;
  const paint = function () {
    if (!status.readable) {
      host.replaceChildren(el('p', 'progress-chart-empty', subject + ' ' + (status.word || 'unknown')));
      return;
    }
    draw(host);
  };
  charts.set(host, paint);
  if (!resizeObserver) {
    resizeObserver = new ResizeObserver(function (entries) {
      entries.forEach(function (entry) {
        const redo = charts.get(entry.target);
        if (redo) redo();
      });
    });
  }
  resizeObserver.observe(host);
  paint();
}

function renderCharts(data, sum) {
  const bucket = data.bucket;
  setNote('progressLocNote', sum.git);
  setNote('progressMrsNote', sum.gitlab);
  setNote('progressIssuesNote', sum.gitlab);
  paintChart('progressChartLoc', sum.git, function (host) {
    drawChart(host, series(data, ['lines_added', 'lines_removed']), {
      label: 'Lines added and removed', bucket: bucket,
      series: [
        { name: 'Added', kind: 'bar', fill: 'var(--success)', get: function (p) { return p.lines_added; } },
        { name: 'Removed', kind: 'bar', fill: 'url(#progressHatch)', get: function (p) { return p.lines_removed == null ? null : -p.lines_removed; } },
      ],
    });
  }, 'Local git history');
  paintChart('progressChartMrs', sum.gitlab, function (host) {
    drawChart(host, series(data, ['mrs_merged']), {
      label: 'Merge requests merged', bucket: bucket,
      series: [{ name: 'Merged', kind: 'bar', fill: 'var(--accent)', get: function (p) { return p.mrs_merged; } }],
    });
  }, 'GitLab totals');
  paintChart('progressChartIssues', sum.gitlab, function (host) {
    drawChart(host, series(data, ['issues_opened', 'issues_closed']), {
      label: 'Issues opened and closed', bucket: bucket,
      series: [
        { name: 'Opened', kind: 'line', color: 'var(--accent)', point: 'circle', get: function (p) { return p.issues_opened; } },
        { name: 'Closed', kind: 'line', color: 'var(--success)', point: 'diamond', dash: '5 3', get: function (p) { return p.issues_closed; } },
      ],
    });
  }, 'GitLab totals');
}

function renderRepos(data) {
  const rows = repoRows(data, view.sort);
  const body = node('progressRepos').querySelector('tbody');
  body.replaceChildren();
  if (!rows.length) {
    const row = el('tr');
    const cell = el('td', 'progress-table-empty', 'No projects read yet');
    cell.colSpan = 6;
    row.appendChild(cell);
    body.appendChild(row);
  }
  rows.forEach(function (r) {
    const row = el('tr');
    row.dataset.repo = r.repo;
    row.appendChild(el('td', 'progress-repo', r.repo));
    [['lines_added', '+', 'progress-pos'], ['lines_removed', '\u2212', 'progress-neg'],
      ['mrs_merged', '', ''], ['issues_opened', '', ''], ['issues_closed', '', '']].forEach(function (col) {
      const word = cellWord(r, col[0]);
      const cell = el('td', 'num ' + (word ? 'progress-missing' : col[2]), word || col[1] + fmtN(r[col[0]]));
      cell.dataset.metric = col[0];
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  node('progressRepos').querySelectorAll('th button').forEach(function (button) {
    if (button.dataset.sort === view.sort) button.setAttribute('aria-sort', 'descending');
    else button.removeAttribute('aria-sort');
  });
  node('progressReposMeta').textContent = reposCaption(data);
}

const SOURCE_COPY = {
  git: {
    what: 'Lines of code',
    label: 'Local git',
    from: 'Each project\u2019s default branch, from its local clone as last fetched. Refresh reads only what is new.',
  },
  gitlab: {
    what: 'MRs and issues',
    label: 'GitLab',
    from: 'Merge requests merged and issues opened and closed, one read per project. Refresh reads only what is new.',
  },
};

const STATE_SENTENCE = {
  stale: 'The last read failed; showing what was kept.',
  unknown: 'Not read yet.',
  unavailable: 'Could not be read right now.',
};

// The lines under a source that say what its figures leave out.
function sourceNotes(key, status) {
  const notes = [];
  if (key === 'gitlab' && status.skipped.length) {
    notes.push('Skipped, no GitLab remote: ' + status.skipped.map(function (s) { return s.repo; }).join(', ') + '.');
  }
  if (key === 'gitlab' && status.truncated.length) {
    notes.push('A full page of 100 was read for ' + status.truncated.join(', ') + '; older days may be incomplete.');
  }
  return notes;
}

function sourceRow(key, status) {
  const copy = SOURCE_COPY[key];
  const row = el('li');
  row.dataset.source = key;
  row.appendChild(el('span', 'progress-source-what', copy.what));
  const tail = status.state === 'ok' || status.state === 'stale'
    ? (status.age || status.word) + (status.state === 'stale' ? ' \u00b7 stale' : '')
    : status.word;
  row.appendChild(chip(copy.label + ' \u00b7 ' + tail, status.state === 'ok' ? 'neutral' : 'attention'));
  row.appendChild(el('span', 'progress-source-from', STATE_SENTENCE[status.state] || copy.from));
  sourceNotes(key, status).forEach(function (note) {
    row.appendChild(el('span', 'progress-source-from', note));
  });
  return row;
}

function renderSources(sum) {
  const host = node('progressSources');
  host.replaceChildren(sourceRow('git', sum.git), sourceRow('gitlab', sum.gitlab));
}

function render() {
  const data = view.data;
  if (!data) return;
  const sum = summary(data);
  node('progressContent').hidden = false;
  const range = currentRange();
  node('progressSheetSub').textContent = rangeText(data.from || range.from, data.to || range.to) +
    (sum.stale ? ' · some figures are stale' : '');
  renderTiles(data);
  renderCharts(data, sum);
  renderRepos(data);
  renderSources(sum);
}

// ------------------------------------------------------------ controls

function press(group, attr, value) {
  node('progressSheet').querySelectorAll('[' + attr + ']').forEach(function (button) {
    if (group.contains(button)) button.setAttribute('aria-pressed', String(button.getAttribute(attr) === value));
  });
}

function syncCustomInputs() {
  const custom = view.range === 'custom';
  node('progressCustom').hidden = !custom;
  if (custom && !view.custom.from) {
    const range = rangeDays('30', null, new Date());
    view.custom = { from: range.from, to: range.to };
  }
  node('progressFrom').value = view.custom.from;
  node('progressTo').value = view.custom.to;
}

function onControl(ev) {
  const button = ev.target.closest('button');
  if (!button) return;
  if (button.dataset.bucket) {
    view.bucket = button.dataset.bucket;
    press(button.parentElement, 'data-bucket', view.bucket);
    load(false);
  } else if (button.dataset.range) {
    view.range = button.dataset.range;
    press(button.parentElement, 'data-range', view.range);
    syncCustomInputs();
    load(false);
  } else if (button.dataset.sort) {
    view.sort = button.dataset.sort;
    if (view.data) renderRepos(view.data);
  }
}

function onCustomChange() {
  view.custom = { from: node('progressFrom').value, to: node('progressTo').value };
  if (rangeDays('custom', view.custom, new Date()).valid) load(false);
}

function close() {
  const d = dialog();
  if (d && d.open) d.close();
}

export function openProgressSheet() {
  const d = dialog();
  if (!d) return;
  if (!d.open) d.showModal();
  syncCustomInputs();
  load(false);
}

export function wireProgressSheet() {
  const d = dialog();
  if (!d) return;
  const opener = document.getElementById('historyOpen');
  if (opener) opener.addEventListener('click', openProgressSheet);
  d.addEventListener('click', onControl);
  node('progressFrom').addEventListener('change', onCustomChange);
  node('progressTo').addEventListener('change', onCustomChange);
  node('progressRefresh').addEventListener('click', refresh);
  node('progressSheetClose').addEventListener('click', close);
  node('progressSheetDone').addEventListener('click', close);
  d.addEventListener('close', function () {
    stopPolling();
    view.seq += 1;
    view.refreshing = false;
    syncRefreshButton();
    hideToast();
    charts.clear();
    if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null; }
  });
}
