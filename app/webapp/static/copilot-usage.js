/* The Code tab's Usage card: the Copilot plan's allowance used this month.
 *
 * One row: a 6px bar filled to the share of the plan's monthly allowance
 * used (the figure the Copilot CLI's own status line shows), a 2px tick at
 * the share of the month gone, the percent and the reset date. Colour
 * follows pace, not raw percent: accent on or under pace, attention ahead of
 * it, danger at 90% or more. A plan nobody could read shows "unknown" with
 * its reason, never 0% and never this machine's share in its place; the
 * footnote keeps this machine's credits today either way.
 *
 * main.js imports this module only after the session list has drawn, so it
 * is not part of the first load, and the server answers from memory
 * (app/webapp/routers/copilot_usage.py), so a slow usage log never holds up
 * this card or anything else. It reads only while the Code tab is on screen.
 */

import { jsonApi } from './api.js';
import { state } from './state.js';
import { startPoll } from './_vendored/poll/poll.js';

const POLL_MS = 60000;
// The server's first read runs in the background; ask again shortly.
const PENDING_RETRY_MS = 2000;
const PENDING_RETRIES = 5;
const DANGER_PCT = 90;

// The plan reading's state, as GET /api/copilot-usage sends it
// (src/copilot_usage.py::payload): OK (a reading), UNKNOWN (the plan could
// not be read; `reason` says why) or PENDING (the server's first read is
// still running). usageState() is the one rule reading the field.
export const UsageState = Object.freeze({
  OK: 'ok',
  UNKNOWN: 'unknown',
  PENDING: 'pending',
});

const KNOWN = new Set(Object.values(UsageState));

// The body's UsageState member; anything outside the set reads as UNKNOWN,
// never as a reading.
export function usageState(body) {
  return body && KNOWN.has(body.state) ? body.state : UsageState.UNKNOWN;
}

const PACE_WORDS = { accent: 'under pace', attention: 'ahead of pace', danger: 'ahead of pace' };

export function usageTone(pct, pace) {
  if (pct == null) return 'none';
  if (pct >= DANGER_PCT) return 'danger';
  if (pace != null && pct > pace) return 'attention';
  return 'accent';
}

export function paceSummary(body) {
  if (usageState(body) !== UsageState.OK) return '';
  return PACE_WORDS[usageTone(body.used_pct, body.pace_pct)] || '';
}

export function resetLabel(iso) {
  const when = new Date(iso);
  if (isNaN(when.getTime())) return '';
  return 'resets ' + when.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

export function formatCredits(n) {
  if (n == null) return '–';
  return n < 10 ? (Math.round(n * 10) / 10).toString() : Math.round(n).toLocaleString('en-US');
}

// This machine's line: its credits today, or why its log could not be read.
function machineText(body) {
  if (body.today_credits != null) return formatCredits(body.today_credits) + ' credits today on this machine.';
  if (body.today_state === UsageState.UNKNOWN) {
    return 'This machine: unknown, ' + (body.today_reason || 'the usage log could not be read') + '.';
  }
  return '';
}

export function footerText(body) {
  if (!body || usageState(body) === UsageState.PENDING) return '–';
  const parts = [];
  if (usageState(body) !== UsageState.OK) {
    parts.push('Plan usage unknown: ' + (body.reason || 'the plan could not be read') + '.');
  }
  const machine = machineText(body);
  if (machine) parts.push(machine);
  return parts.join(' ') || '–';
}

export function rowTitle(body) {
  const lines = [];
  if (usageState(body) === UsageState.OK) {
    lines.push(formatCredits(body.used_credits) + ' of ' + formatCredits(body.allowance)
      + ' AI credits used this month on the plan.');
  } else {
    lines.push('Plan usage unknown. Allowance from Settings: ' + formatCredits(body.allowance) + ' AI credits.');
  }
  lines.push(body.pace_pct + '% of the month gone.');
  if (body.machine_month_credits != null) {
    lines.push(formatCredits(body.machine_month_credits) + ' credits this month on this machine.');
  }
  lines.push('Source: ' + body.source + ', counting ' + body.scope + '.');
  return lines.join('\n');
}

function el(tag, cls) {
  const node = document.createElement(tag);
  node.className = cls;
  return node;
}

export function renderUsage(body) {
  const meter = document.getElementById('codingUsage');
  if (!meter || !body || usageState(body) === UsageState.PENDING) return;
  const row = meter.querySelector('.um-row');
  const bar = meter.querySelector('.um-bar');
  const pctEl = meter.querySelector('.um-pct');
  const reset = meter.querySelector('.um-reset');
  const ok = usageState(body) === UsageState.OK && body.used_pct != null;
  const tone = ok ? usageTone(body.used_pct, body.pace_pct) : 'none';
  meter.removeAttribute('data-pending');
  row.dataset.tone = tone;
  row.title = rowTitle(body);
  bar.replaceChildren();
  if (ok) {
    const fill = el('span', 'um-fill');
    fill.dataset.tone = tone;
    fill.style.width = Math.min(100, body.used_pct) + '%';
    bar.appendChild(fill);
  }
  const tick = el('span', 'um-tick');
  tick.style.left = body.pace_pct + '%';
  bar.appendChild(tick);
  bar.setAttribute('role', 'meter');
  bar.setAttribute('aria-label', 'Copilot plan allowance used this month');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');
  if (ok) {
    bar.setAttribute('aria-valuenow', String(Math.min(100, body.used_pct)));
    bar.removeAttribute('aria-valuetext');
  } else {
    bar.removeAttribute('aria-valuenow');
    bar.setAttribute('aria-valuetext', 'unknown');
  }
  pctEl.textContent = ok ? Math.round(body.used_pct) + '%' : 'unknown';
  reset.textContent = resetLabel(body.reset_at);
  const meta = document.getElementById('codingUsageMeta');
  if (meta) {
    meta.textContent = paceSummary(body);
    meta.dataset.tone = ok ? tone : 'none';
  }
  const footer = document.getElementById('codingUsageFooter');
  if (footer) footer.textContent = footerText(body);
}

let timer = null;
let inFlight = false;

function codeTabShown() {
  return !document.hidden && state.tab === 'claude';
}

async function fetchUsage(retries) {
  if (inFlight || !codeTabShown()) return;
  inFlight = true;
  let body = null;
  try {
    body = await jsonApi('/api/copilot-usage');
  } catch (exc) {
    // The card keeps its last reading; the next poll tries again.
    console.warn('copilot usage read failed', exc);
  } finally {
    inFlight = false;
  }
  if (!body) return;
  renderUsage(body);
  if (usageState(body) === UsageState.PENDING && retries > 0) {
    setTimeout(function () { fetchUsage(retries - 1); }, PENDING_RETRY_MS);
  }
}

export function wireCopilotUsage() {
  if (timer !== null) return;
  fetchUsage(PENDING_RETRIES);
  timer = startPoll(function () { return fetchUsage(0); }, POLL_MS);
  document.addEventListener('launcher:tab', function (ev) {
    if (ev.detail && ev.detail.tab === 'claude') fetchUsage(PENDING_RETRIES);
  });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) fetchUsage(PENDING_RETRIES);
  });
}
