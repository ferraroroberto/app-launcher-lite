/* The page header's CPU reading: "is the box loaded?" at a glance
 * on every tab, and "by what?" one tap away.
 *
 * Every pane's home-head carries a .cpu-reading button left of the theme
 * toggle. It is a quiet muted number at rest; colour only rings an
 * exception — attention once CPU stays at or above 70% for ~30 s, danger at
 * or above 90% — and a reading the server could not take shows "–", never
 * "0%". The level is the server's (src/system_load.py), so the rule lives in
 * one place.
 *
 * One small poll for the whole page, never one per tab: the server answers
 * from its shared sampler's cached sample, and the poll skips while the
 * page is hidden. Coming back to the foreground reads once at once. While
 * the sampler is still taking its first sample, one short retry follows.
 *
 * The sheet also shows the PC's private IP with a Copy action. The address
 * comes from the /api/status payload the page already holds and is re-read
 * (POST /api/status/refresh) each time the sheet opens; one that could not be
 * read is "Unknown" and Copy is disabled.
 */

import { SYSTEM_LOAD_WARMING_RETRY_MS, state } from './state.js';
import { jsonApi, logPollFailure } from './api.js';
import { el, sheetRow } from './dom-utils.js';
import { pctText } from './shared/format.js';
import { copyText } from './apps/apps-listeners.js';

// The last payload; null when the fetch itself failed. Before the first
// answer it reads as the server's own "still taking the first sample".
let reading = { level: 'unknown', reason: 'warming' };
let retryTimer = null;

function readingKnown(r) {
  return !!(r && r.level !== 'unknown' && typeof r.cpu_percent === 'number');
}

// Why a reading is unknown, in words: each condition its own message.
function unknownText(r) {
  const reason = r ? r.reason : 'fetch';
  if (reason === 'warming') return 'Reading the CPU…';
  if (reason === 'stale') return 'Last reading is too old';
  if (reason === 'error') return "Couldn't read the CPU";
  return "Couldn't reach the launcher";
}

function gb(bytes) {
  return (bytes / 1024 ** 3).toFixed(1).replace(/\.0$/, '');
}

function coresText(n) {
  const v = Math.round(n * 10) / 10;
  return v + (v === 1 ? ' core' : ' cores');
}

function renderReadings() {
  const known = readingKnown(reading);
  const level = known ? reading.level : 'unknown';
  const text = known ? pctText(reading.cpu_percent) : '–';
  const label = known
    ? 'CPU ' + text + (level === 'attention' ? ', high for 30 seconds' : '')
      + (level === 'danger' ? ', very high' : '') + '. Show the busiest processes'
    : 'CPU load unknown. Show details';
  document.querySelectorAll('.cpu-reading').forEach(function (btn) {
    btn.textContent = text;
    btn.dataset.level = level;
    btn.setAttribute('aria-label', label);
  });
}

// The PC's private IP and a Copy that puts exactly that string on the
// clipboard. First in the sheet, so it shows whether or not the CPU read.
// One node for the page's life, updated in place: the sheet's other rows are
// rebuilt on every poll, and a button replaced between press and release
// never receives its click.
let ipRow = null;

function ipRowNode() {
  if (ipRow) return ipRow;
  ipRow = sheetRow('IP', 'Unknown', 'ip');
  const btn = el('button', 'button-ghost', 'Copy');
  btn.type = 'button';
  btn.dataset.track = 'cpu.copy-ip';
  btn.addEventListener('click', function () {
    const ip = state.status && state.status.private_ip;
    if (ip) copyText(ip, 'IP address', { host: document.getElementById('cpuSheetToast') });
  });
  ipRow.appendChild(btn);
  return ipRow;
}

function syncIpRow() {
  const row = ipRowNode();
  const ip = state.status && state.status.private_ip;
  row.querySelector('.usage-sheet-value').textContent = ip || 'Unknown';
  row.querySelector('button').disabled = !ip;
  return row;
}

// Keeps the IP row attached and replaces everything after it.
function resetSheetBody(body) {
  const row = syncIpRow();
  if (row.parentNode !== body) {
    body.replaceChildren(row);
    return;
  }
  while (body.lastChild !== row) body.lastChild.remove();
}

// Re-read the address when the sheet opens, so a changed one shows without a
// reload. A failed read, or one that found nothing, keeps the last value.
async function refreshIp() {
  let body;
  try {
    body = await jsonApi('/api/status/refresh', { method: 'POST' });
  } catch (exc) {
    logPollFailure('private ip refresh failed', exc);
    return;
  }
  if (!body || !body.discovery_ok) return;
  state.status = Object.assign({}, state.status, { private_ip: body.private_ip });
  const dialog = sheet();
  if (dialog && dialog.open) renderSheet();
}

function renderSheet() {
  const body = document.getElementById('cpuSheetBody');
  if (!body) return;
  resetSheetBody(body);
  if (!readingKnown(reading)) {
    body.appendChild(sheetRow('CPU', unknownText(reading), 'cpu'));
    return;
  }
  const r = reading;
  const cpu = r.cores ? pctText(r.cpu_percent) + ' of ' + r.cores + ' cores' : pctText(r.cpu_percent);
  const cpuRow = sheetRow('CPU', cpu, 'cpu');
  if (r.level !== 'normal') cpuRow.lastChild.dataset.tone = r.level;
  body.appendChild(cpuRow);
  const mem = r.memory;
  body.appendChild(sheetRow(
    'Memory',
    mem && mem.total_bytes
      ? gb(mem.used_bytes) + ' of ' + gb(mem.total_bytes) + ' GB (' + pctText(mem.percent) + ')'
      : 'Unknown',
    'memory'
  ));
  body.appendChild(el('p', 'cpu-sheet-caption', 'Busiest processes, last few seconds'));
  if (!r.top || !r.top.length) {
    body.appendChild(sheetRow('Processes', 'None busy', 'top-none'));
    return;
  }
  r.top.forEach(function (p, i) {
    body.appendChild(sheetRow(groupLabel(p), coresText(p.cores), 'top-' + i));
  });
  // What the rows above do not explain, so they add up to the CPU line.
  // Hidden when it rounds to nothing.
  if (typeof r.other_cores === 'number' && Math.round(r.other_cores * 10) > 0) {
    body.appendChild(sheetRow('Everything else', coresText(r.other_cores), 'other'));
  }
}

// "python ×16 · fleet-config-wt-1440": a count only for a real group (never
// "×1"), and the repo or worktree folder when the server resolved one.
function groupLabel(p) {
  return p.name + (p.count > 1 ? ' ×' + p.count : '') + (p.owner ? ' · ' + p.owner : '');
}

function sheet() {
  return document.getElementById('cpuSheet');
}

function render() {
  renderReadings();
  const dialog = sheet();
  if (dialog && dialog.open) renderSheet();
}

export async function fetchSystemLoad() {
  if (document.hidden) return;
  try {
    reading = await jsonApi('/api/system/load');
  } catch (exc) {
    reading = null;
    render();
    logPollFailure('system load poll failed', exc);
    return;
  }
  render();
  // The sampler's first sample lands ~2 s after the first read wakes it:
  // one short retry instead of a "–" for a whole poll.
  if (reading && reading.reason === 'warming' && !retryTimer) {
    retryTimer = setTimeout(function () {
      retryTimer = null;
      fetchSystemLoad().catch(function () {});
    }, SYSTEM_LOAD_WARMING_RETRY_MS);
  }
}

function openSheet() {
  const dialog = sheet();
  if (!dialog) return;
  renderSheet();
  if (!dialog.open) dialog.showModal();
  refreshIp().catch(function () {});
}

function closeSheet() {
  const dialog = sheet();
  if (dialog && dialog.open) dialog.close();
}

// The poll itself is armed by main.js after the boot fetch, like every
// other poll; this wires the taps and the foreground read.
export function wireSystemLoad() {
  renderReadings();
  document.querySelectorAll('.cpu-reading').forEach(function (btn) {
    btn.addEventListener('click', openSheet);
  });
  const close = document.getElementById('cpuSheetClose');
  const done = document.getElementById('cpuSheetDone');
  if (close) close.addEventListener('click', closeSheet);
  if (done) done.addEventListener('click', closeSheet);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) fetchSystemLoad().catch(function () {});
  });
}
