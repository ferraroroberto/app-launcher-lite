/* Settings > Performance.
 *
 * A slower computer needs other poll spacings, pool sizes and cache lifetimes
 * than a fast one, so every speed limit is a field here. The server owns the
 * table (src/webapp_settings/performance.py): GET /api/config carries each
 * limit's label, default, hard range and help, the two presets (Standard, and
 * Light machine for a slow PC) and which preset the stored values equal, so
 * this sheet draws from that and a limit added there appears here unedited.
 *
 * Every field saves when it is left; a value the server refuses stays in the
 * field with the reason under it. A preset saves all limits in one write. A
 * set that equals neither preset reads Custom. Nothing needs a restart: the
 * polls read the values at their next tick and the server resizes its pools.
 *
 * Under the presets one read-only line (GET /api/performance/health) shows
 * whether the box is coping: CPU, free memory, requests in flight, agent
 * processes and live lanes against the worker cap. A figure it could not read
 * says unknown, never zero.
 */
import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { setSwitch } from '../_vendored/switch/switch.js';
import { fetchConfig } from './claude-options.js';

const PRESET_NAMES = { standard: 'Standard', light: 'Light machine', custom: 'Custom' };
const GROUP_TITLES = {
  polls: 'Refresh',
  pools: 'Worker threads',
  caches: 'Caches and budgets',
};
// How often the health line refreshes while the sheet is open.
const HEALTH_POLL_MS = 5000;

let drawn = false;
let healthTimer = null;
const fields = new Map();

export function performanceRowValue(cfg) {
  const perf = cfg && cfg.performance;
  return perf ? PRESET_NAMES[perf.preset] || '' : '';
}

function unitWord(knob) {
  return knob.unit ? ' (' + knob.unit + ')' : '';
}

function defaultText(knob) {
  if (knob.kind === 'bool') return knob.default ? 'on' : 'off';
  if (knob.auto_zero && knob.default === 0) return 'automatic';
  return String(knob.default) + (knob.unit ? ' ' + knob.unit : '');
}

function setError(entry, message) {
  entry.error.textContent = message || '';
  entry.error.hidden = !message;
  if (message) entry.control.setAttribute('aria-invalid', 'true');
  else entry.control.removeAttribute('aria-invalid');
}

// POST one change; resolves true once saved and the config is re-read.
async function save(patch, entry) {
  if (entry) setError(entry, '');
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
  } catch (exc) {
    // The server words the refusal (the range and why): show it by the field.
    if (entry && exc && exc.status === 400) setError(entry, exc.message);
    else apiFailToast('Performance not saved', exc);
    return false;
  }
  await fetchConfig().catch(function () {});
  return true;
}

function numberField(knob) {
  const label = document.createElement('label');
  label.className = 'stacked';
  const name = document.createElement('span');
  name.textContent = knob.label + unitWord(knob);
  const input = document.createElement('input');
  input.type = 'number';
  input.className = 'input-native';
  input.inputMode = knob.kind === 'int' ? 'numeric' : 'decimal';
  input.step = knob.kind === 'int' ? '1' : 'any';
  input.min = knob.min;
  input.max = knob.max;
  input.dataset.perfKey = knob.key;
  input.dataset.track = 'performance.field';
  label.append(name, input);
  return { wrap: label, control: input };
}

function switchField(knob) {
  const row = document.createElement('span');
  row.className = 'switch-row';
  const name = document.createElement('span');
  name.textContent = knob.label;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'toggle';
  button.setAttribute('role', 'switch');
  button.setAttribute('aria-label', knob.label);
  button.dataset.perfKey = knob.key;
  button.dataset.track = 'performance.switch';
  row.append(name, button);
  return { wrap: row, control: button };
}

function wireNumber(knob, entry) {
  const input = entry.control;
  input.addEventListener('change', function () {
    const raw = input.value.trim();
    const value = Number(raw);
    if (raw === '' || !Number.isFinite(value)) {
      setError(entry, knob.label + ' must be a number.');
      return;
    }
    save({ [knob.key]: value }, entry).then(function (ok) {
      if (ok) toast(knob.label + ' saved.', 'good');
    });
  });
}

function wireSwitch(knob, entry) {
  const button = entry.control;
  button.addEventListener('click', async function () {
    const next = button.getAttribute('aria-checked') !== 'true';
    setSwitch(button, next);
    if (await save({ [knob.key]: next }, entry)) {
      toast(knob.label + (next ? ' on.' : ' off.'), 'good');
    } else {
      setSwitch(button, !next);
    }
  });
}

function draw(perf) {
  const host = els.performanceFields;
  host.textContent = '';
  let group = '';
  perf.knobs.forEach(function (knob) {
    if (knob.group !== group) {
      group = knob.group;
      const title = document.createElement('h3');
      title.className = 'settings-overline';
      title.textContent = GROUP_TITLES[group] || group;
      host.appendChild(title);
    }
    const isSwitch = knob.kind === 'bool';
    const entry = isSwitch ? switchField(knob) : numberField(knob);
    const help = document.createElement('p');
    help.className = 'field-help';
    help.id = 'perfHelp-' + knob.key;
    help.textContent = knob.help + ' Default: ' + defaultText(knob) + '.';
    entry.control.setAttribute('aria-describedby', help.id);
    entry.error = document.createElement('p');
    entry.error.className = 'field-error';
    entry.error.setAttribute('role', 'alert');
    entry.error.hidden = true;
    entry.knob = knob;
    fields.set(knob.key, entry);
    host.append(entry.wrap, help, entry.error);
    if (isSwitch) wireSwitch(knob, entry);
    else wireNumber(knob, entry);
  });
  drawn = true;
}

function paintValues(perf) {
  perf.knobs.forEach(function (knob) {
    const entry = fields.get(knob.key);
    if (!entry) return;
    const value = perf.values[knob.key];
    if (knob.kind === 'bool') {
      setSwitch(entry.control, value === true);
    } else if (entry.control !== document.activeElement) {
      entry.control.value = String(value);
    }
  });
}

function paintPreset(perf) {
  document.querySelectorAll('#performancePreset [data-preset]').forEach(function (btn) {
    const on = btn.dataset.preset === perf.preset;
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    // Custom is a state, not an action: it can be read but never tapped.
    if (btn.dataset.preset === 'custom') btn.disabled = !on;
  });
  if (els.performancePresetHelp) {
    els.performancePresetHelp.textContent = perf.preset === 'custom'
      ? 'Your own values: they match neither preset. Choosing a preset replaces them all.'
      : perf.preset === 'light'
        ? 'Light machine spaces refreshes out, shrinks the worker pools and stretches the caches, for a slower PC.'
        : 'Standard is the shipped defaults, right for an ordinary PC. Light machine suits a slow one.';
  }
}

export function renderPerformance(cfg) {
  const perf = cfg && cfg.performance;
  if (!perf || !els.performanceFields) return;
  if (!drawn) draw(perf);
  paintValues(perf);
  paintPreset(perf);
}

// One line of figures; a figure the server could not read says "unknown" and
// its reason follows the line, so the reading never shows a zero it did not take.
function healthText(h) {
  const unknown = h.unknown || {};
  const part = function (key, name, text) { return unknown[key] ? name + ' unknown' : text; };
  const figures = [
    part('cpu', 'CPU', 'CPU ' + Math.round(h.cpu_percent) + '%'),
    part('memory', 'memory', h.memory_free_gb + ' GB free of ' + h.memory_total_gb + ' GB'),
    h.inflight + ' in flight (peak ' + h.max_inflight + ')',
    part('agents', 'agent processes', h.agent_processes + (h.agent_processes === 1 ? ' agent process' : ' agent processes')),
    part('lanes', 'lanes', h.lanes + ' of ' + h.worker_cap + ' lanes'),
  ].join(' · ');
  const reasons = Array.from(new Set(Object.values(unknown)));
  return 'Now: ' + figures + (reasons.length ? '. Unknown because ' + reasons.join('; ') + '.' : '');
}

async function refreshHealth() {
  const out = els.performanceHealth;
  if (!out) return;
  try {
    out.textContent = healthText(await jsonApi('/api/performance/health'));
  } catch (_) {
    out.textContent = 'Health: unknown (the webapp did not answer).';
  }
}

function stopHealth() {
  window.clearInterval(healthTimer);
  healthTimer = null;
}

function startHealth() {
  stopHealth();
  refreshHealth();
  healthTimer = window.setInterval(function () {
    if (!document.hidden) refreshHealth();
  }, HEALTH_POLL_MS);
}

export function wirePerformance() {
  const sheet = document.getElementById('performanceSheet');
  if (!sheet) return;
  // The health line reads only while the sheet is open; it is not a page poll.
  new MutationObserver(function () {
    if (sheet.open) startHealth();
    else stopHealth();
  }).observe(sheet, { attributes: true, attributeFilter: ['open'] });
  document.querySelectorAll('#performancePreset [data-preset]').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      const perf = state.config && state.config.performance;
      const name = btn.dataset.preset;
      if (!perf || !perf.presets[name]) return;
      if (await save(perf.presets[name], null)) {
        toast(PRESET_NAMES[name] + ' applied.', 'good');
      }
    });
  });
}
