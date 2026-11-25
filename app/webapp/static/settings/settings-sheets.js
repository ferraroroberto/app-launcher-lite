/* Settings as inset groups.
 *
 * The Settings pane is a stack of rows: a lead icon, a label, the current
 * value in muted text and a chevron. Each row opens a sheet, a <dialog> on
 * the vendored modal shell with a header back + ✕ and one full-width Done
 * Text size and Start at log on stay inline on their rows.
 *
 * This module owns three things:
 * - the sheets' navigation: the open sheets are a stack. Back pops one
 *   level, ✕ and Done close them all, Escape goes back. Only the top sheet
 *   is shown; its parent reopens on Back.
 * - the rows' values, recomputed from state whenever the data behind them
 *   lands (config, passkeys, tokens) and whenever a sheet closes.
 * - the fields that used to wait for a Save button: every one saves as it
 *   changes (a text or number field on blur), with a toast, and an inline
 *   error under the field when it is out of range or the server refuses it.
 *
 * The sheets' other controls keep their own modules: claude-options.js
 * (Coding agent), tokens.js and webauthn.js (Passkeys).
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { revealInCard } from '../dom-utils.js';
import { wireModelCombo } from '../shared/model-combo.js';
import { choice, fetchConfig, fillSelect, patchConfig, wireConfigSwitch } from './claude-options.js';
import { terminalJsonApi } from '../webauthn.js';
import { setSwitch } from '../_vendored/switch/switch.js';
import { fetchApps, fetchSkills } from '../lazy-views.js';
import { plural } from '../shared/format.js';
import { ACTION_ROW_MENU } from '../action-rows.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';
import { icon } from '../_vendored/icons/icons.js';
import { servingValue, wireServing } from './serving.js';
import { speechValue, wireSpeech } from './speech-settings.js';
import { performanceRowValue } from './performance.js';
import { wireMirrorBrowser } from './mirror-browser.js';

// ------------------------------------------------------------ navigation
// The open sheets, the shown one last.
const stack = [];

function sheet(id) {
  return document.getElementById(id);
}

function closeTop() {
  const top = sheet(stack[stack.length - 1]);
  if (top && top.open) top.close();
}

// Show the sheet path `ids`: the current top closes and the path's last
// sheet opens.
export function showPath(ids) {
  const target = ids[ids.length - 1];
  if (stack[stack.length - 1] !== target) closeTop();
  stack.splice(0, stack.length, ...ids);
  const d = sheet(target);
  if (d && !d.open) d.showModal();
}

function back() {
  closeTop();
  stack.pop();
  const parent = sheet(stack[stack.length - 1]);
  if (parent) {
    parent.showModal();
  } else {
    renderSettingsValues();
  }
}

function closeSheets() {
  closeTop();
  stack.length = 0;
  renderSettingsValues();
}

// Land on one Settings field or sheet (via tabs.js's
// openSettingsAt): open the sheet that holds it, then scroll to and focus
// the field. A sheet id opens just that sheet.
export function revealSettingsField(el) {
  if (!el) return;
  const host = el.matches('dialog.settings-sheet') ? el : el.closest('dialog.settings-sheet');
  if (!host) {
    revealInCard(el);
    return;
  }
  showPath([host.id]);
  if (el !== host) revealInCard(el);
}

// ----------------------------------------------------------------- values
function joined(parts) {
  return parts.filter(Boolean).join(' · ');
}

// A Copilot model id as a row value: "" is Copilot's own pick.
function modelWord(model) {
  return model || 'Default';
}

// The Coding agent row: "gpt-5.6-luna · high · autopilot".
function copilotValue(c) {
  if (!c) return '';
  return joined([
    modelWord(c.model),
    c.model ? c.effort : '',
    c.autopilot ? 'autopilot' : '',
    c.skip_permissions ? 'skip prompts' : '',
  ]);
}

// `tone` (optional) colours an exception, e.g. 'attention'; a plain value
// clears it.
function setValue(key, text, tone) {
  document.querySelectorAll('[data-settings-value="' + key + '"]').forEach(function (el) {
    el.textContent = text;
    if (tone) el.dataset.tone = tone;
    else delete el.dataset.tone;
  });
}

function passkeysValue() {
  const w = state.webauthn || {};
  if (!w.configured) return 'Off';
  const n = (w.devices || []).length;
  return n ? plural(n, 'device', 'devices') : 'None';
}

function tokensValue() {
  const n = state.apiTokenCount;
  if (n == null) return '';
  return n ? plural(n, 'token', 'tokens') : 'None';
}

export function renderSettingsValues() {
  const cfg = state.config;
  setValue('tokens', tokensValue());
  setValue('passkeys', passkeysValue());
  if (!cfg) return;
  setValue('copilot', copilotValue(cfg.copilot));
  setValue('speech', speechValue(cfg.speech));
  setValue('serving', servingValue(cfg.serving));
  setValue('performance', performanceRowValue(cfg));
  setValue('gitlab', cfg.gitlab_group
    ? joined([cfg.gitlab_group, cfg.gitlab_host || ''])
    : 'Not set');
  const cap = Number(cfg.chief_worker_cap) || 0;
  setValue('chief', joined([modelWord(cfg.chief_model), cap ? plural(cap, 'worker', 'workers') : '']));
  const folders = [cfg.projects_dir, cfg.apps_scan_root, cfg.team_os_dir, cfg.fleet_config_dir]
    .filter(function (v) { return v && String(v).trim(); }).length;
  setValue('folders', folders + ' set');
  renderOutboxRoots(cfg.outbox_scratch_roots || []);
  setValue('terminal', joined([
    cfg.terminal_history_lines ? cfg.terminal_history_lines + ' lines' : '',
    cfg.large_upload_max_mb ? cfg.large_upload_max_mb + ' MB' : '',
  ]));
  renderChiefFields(cfg);
}

// --------------------------------------------------------- saving fields
// An inline error line under a field: created on first use, after the
// field's help line, and named by the field's aria-describedby.
function setFieldError(input, message) {
  const id = input.id + 'Error';
  let err = document.getElementById(id);
  if (!err && message) {
    err = document.createElement('p');
    err.id = id;
    err.className = 'field-error';
    err.setAttribute('role', 'alert');
    const helpId = (input.getAttribute('aria-describedby') || '').split(' ')[0];
    const anchor = document.getElementById(helpId) || input.closest('label') || input;
    anchor.after(err);
    input.setAttribute('aria-describedby',
      ((input.getAttribute('aria-describedby') || '') + ' ' + id).trim());
  }
  if (err) {
    err.textContent = message || '';
    err.hidden = !message;
  }
  if (message) {
    input.setAttribute('aria-invalid', 'true');
  } else {
    input.removeAttribute('aria-invalid');
  }
}

// A whole number inside the field's own min/max (the server enforces the
// same range); null after saying what is wrong.
function wholeNumberIn(input, name) {
  const min = Number(input.min);
  const max = Number(input.max);
  const value = Number(input.value);
  if (input.value.trim() === '' || !Number.isInteger(value) || value < min || value > max) {
    const message = name + ' must be a whole number between ' + min + ' and ' + max + '.';
    setFieldError(input, message);
    toast(message, 'error');
    return null;
  }
  return value;
}

async function saveConfigField(input, patch, message, after) {
  setFieldError(input, '');
  if (!(await patchConfig(patch))) {
    // patchConfig already toasted the server's reason.
    setFieldError(input, 'Not saved. Check the value and try again.');
    return;
  }
  toast(message, 'good');
  if (after) await after().catch(function () {});
}

// ---------------------------------------------------- chat file folders
// The folders an agent may send Chat files from, one row each. Every
// add or remove posts the whole new list and the rows are redrawn from the
// server's answer (fetchConfig), so a refused entry never lingers on screen.
// The last row cannot be removed: an empty list would read as unset on the
// server and bring the default back, a state this editor must not offer.
// Remove sits in the row's menu, never as a visible control on the row.
const outboxMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });

function renderOutboxRoots(roots) {
  const list = els.outboxRootsList;
  if (!list) return;
  list.textContent = '';
  roots.forEach(function (root) {
    const li = document.createElement('li');
    const path = document.createElement('span');
    path.className = 'outbox-root-path';
    path.textContent = root;
    const kebab = document.createElement('button');
    kebab.dataset.track = 'folders.outbox-menu';
    kebab.type = 'button';
    kebab.className = 'icon-button action-row-kebab';
    kebab.title = 'Folder actions';
    kebab.setAttribute('aria-label', 'Folder actions');
    kebab.innerHTML = icon('ellipsis-vertical');
    const menuEl = outboxMenu.attach(root, kebab, [{
      dataset: { item: 'outbox-remove-item' }, glyph: 'trash-2', danger: true,
      label: 'Remove', ariaLabel: 'Remove ' + root,
      disabled: roots.length < 2,
      hint: roots.length < 2 ? 'Keep at least one folder' : '',
      onTap: function () {
        saveOutboxRoots(roots.filter(function (r) { return r !== root; }), 'Chat file folder removed.');
      },
    }]);
    if (menuEl) menuEl.dataset.surface = 'settings.outbox-menu';
    li.append(path, kebab);
    list.appendChild(li);
  });
  outboxMenu.endRender();
}

async function saveOutboxRoots(roots, message) {
  const input = els.outboxRootInput;
  setFieldError(input, '');
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outbox_scratch_roots: roots }),
    });
  } catch (exc) {
    // The server words each refusal (network path, drive root, missing,
    // duplicate...): show it under the field as well as in the toast.
    setFieldError(input, (exc && exc.message) || 'Not saved. Check the folder and try again.');
    apiFailToast('Folder not saved', exc);
    return;
  }
  input.value = '';
  await fetchConfig().catch(function () {});
  toast(message, 'good');
}

function wireOutboxRoots() {
  const input = els.outboxRootInput;
  const add = els.outboxRootAdd;
  if (!input || !add) return;
  function submit() {
    const value = input.value.trim();
    if (!value) {
      setFieldError(input, 'Enter a folder path.');
      return;
    }
    const current = (state.config && state.config.outbox_scratch_roots) || [];
    saveOutboxRoots(current.concat(value), 'Chat file folder added.');
  }
  add.addEventListener('click', submit);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  });
}

// [field id, config key, name for the toast, refresh after a save]
const TEXT_FIELDS = [
  ['projectsDir', 'projects_dir', 'Projects folder', function () { return fetchApps(); }],
  ['appsScanRoot', 'apps_scan_root', 'Apps folder', null],
  ['teamOsDir', 'team_os_dir', 'Team OS folder', function () { return fetchSkills(); }],
  ['fleetConfigDir', 'fleet_config_dir', 'Copilot folder', null],
  ['skillsDir', 'skills_dir', 'Skills folder', null],
  ['gitlabGroup', 'gitlab_group', 'GitLab group', null],
  ['gitlabHost', 'gitlab_host', 'GitLab host', null],
];
const NUMBER_FIELDS = [
  ['terminalHistoryLines', 'terminal_history_lines', 'Terminal history'],
  ['largeUploadMaxMb', 'large_upload_max_mb', 'Large file limit'],
  ['copilotMonthlyCredits', 'copilot_monthly_credits', 'Monthly allowance'],
  ['speechKeepHours', 'speech_keep_recordings_hours', 'Keep recordings'],
];

// `change` fires once an edited field loses focus (or on Enter), never per
// keystroke, so a half-typed path is never saved.
function wireFields() {
  TEXT_FIELDS.forEach(function (f) {
    const input = document.getElementById(f[0]);
    if (!input) return;
    input.addEventListener('change', function () {
      saveConfigField(input, { [f[1]]: input.value.trim() }, f[2] + ' saved.', f[3]);
    });
  });
  if (els.projectsIgnore) {
    els.projectsIgnore.addEventListener('change', function () {
      const ignore = els.projectsIgnore.value
        .split('\n')
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      saveConfigField(els.projectsIgnore, { projects_ignore: ignore }, 'Hidden projects saved.',
        function () { return fetchApps(); });
    });
  }
  NUMBER_FIELDS.forEach(function (f) {
    const input = document.getElementById(f[0]);
    if (!input) return;
    input.addEventListener('change', function () {
      const value = wholeNumberIn(input, f[2]);
      if (value === null) return;
      saveConfigField(input, { [f[1]]: value }, f[2] + ' saved.');
    });
  });
  wireOutboxRoots();
}

// ------------------------------------------------------------------ chief
// Model and worker cap are the chief's own settings (PUT
// /api/board/chief/settings, passkey-gated: they steer it); auto-compact is
// a config field the chief reads. GET /api/config carries all four,
// so the row's value needs no passkey.
let chiefModelCombo = null;

// Default ("" — Copilot picks) plus the config's Copilot models; a stored
// model since dropped from that list still shows as itself.
// One line per issue skill: found (and from where), missing, or unknown with why.
// Unknown is its own word, never shown as found.
function renderSkillsPreflight(preflight) {
  const out = els.skillsDirStatus;
  if (!out) return;
  if (!preflight || !Array.isArray(preflight.skills)) {
    out.textContent = 'Issue skills: not checked.';
    return;
  }
  const from = preflight.source === 'skills_dir' ? 'from the skills folder' : 'bundled';
  // Skills that share a state and reason (a missing folder fails all four alike) read as one line.
  const groups = [];
  preflight.skills.forEach(function (s) {
    const what = s.state === 'found' ? 'found (' + from + ')' : s.state + (s.reason ? ', ' + s.reason : '');
    const same = groups.find(function (g) { return g.what === what; });
    if (same) same.names.push(s.name); else groups.push({ what: what, names: [s.name] });
  });
  out.textContent = groups.map(function (g) { return g.names.join(', ') + ': ' + g.what; }).join('. ') + '.';
}

function renderChiefFields(cfg) {
  renderSkillsPreflight(cfg.skills_preflight);
  if (chiefModelCombo) {
    const models = ((cfg.copilot || {}).models_available || []).slice();
    const current = cfg.chief_model || '';
    const dropped = current && current !== 'default' && models.indexOf(current) === -1;
    if (dropped) models.push(current);
    chiefModelCombo.setOptions([{ value: '', label: 'Default' }].concat(
      models.map(function (m) {
        return { value: m, label: m === current && dropped ? m + ' (not offered)' : m };
      })
    ));
    chiefModelCombo.setValue(current);
  }
  renderChiefLaunchFields(cfg);
  // On = the cap counts only the chief's own sessions; a missing value is that.
  if (els.chiefWorkerCapScopeToggle) {
    setSwitch(els.chiefWorkerCapScopeToggle, cfg.chief_worker_cap_scope !== 'all');
  }
  const cap = els.chiefWorkerCap;
  if (!cap) return;
  if (cfg.chief_worker_cap_min != null) cap.min = cfg.chief_worker_cap_min;
  if (cfg.chief_worker_cap_max != null) cap.max = cfg.chief_worker_cap_max;
  if (els.chiefWorkerCapHelp && cfg.chief_worker_cap_min != null && cfg.chief_worker_cap_max != null) {
    els.chiefWorkerCapHelp.textContent = 'The only limit on how many workers the chief runs at once, from '
      + cfg.chief_worker_cap_min + ' to ' + cfg.chief_worker_cap_max + '.';
  }
  if (cap !== document.activeElement && cfg.chief_worker_cap != null) {
    cap.value = String(cfg.chief_worker_cap);
  }
}

// "" follows the Coding agent sheet, so the first option says that, not "Default".
function chiefChoice(value) {
  return value ? choice(value) : { value: '', label: 'Same as Coding agent' };
}

// Context and effort beside the model; a saved model the list no longer offers
// is called out, since a start would fall back to Copilot's own pick.
function renderChiefLaunchFields(cfg) {
  const c = cfg.copilot || {};
  if (els.chiefModelProblem) {
    els.chiefModelProblem.textContent = cfg.chief_model_problem || '';
    els.chiefModelProblem.hidden = !cfg.chief_model_problem;
  }
  fillSelect(els.chiefContext, (c.contexts_available || []).map(chiefChoice),
    cfg.chief_context || '', function (v) {
      putChiefSettings({ context: v }, 'Chief context: ' + chiefChoice(v).label + '.', els.chiefContext);
    });
  fillSelect(els.chiefEffort, (c.efforts_available || []).map(chiefChoice),
    cfg.chief_effort || '', function (v) {
      putChiefSettings({ effort: v }, 'Chief effort: ' + chiefChoice(v).label + '.', els.chiefEffort);
    });
  // Effort goes out only beside an explicit, offered model.
  if (els.chiefEffort) {
    els.chiefEffort.disabled = !cfg.chief_model || cfg.chief_model === 'default'
      || !!cfg.chief_model_problem;
  }
}

async function putChiefSettings(patch, message, input) {
  try {
    await terminalJsonApi('/api/board/chief/settings', { method: 'PUT', body: patch });
  } catch (exc) {
    if (input) setFieldError(input, 'Not saved. Check the value and try again.');
    apiFailToast('Chief settings save failed', exc);
    // Put the controls back on what is stored.
    await fetchConfig().catch(function () {});
    return;
  }
  if (input) setFieldError(input, '');
  await fetchConfig().catch(function () {});
  toast(message, 'good');
}

function wireChief() {
  chiefModelCombo = wireModelCombo(els.chiefModelSelect, function (model) {
    putChiefSettings({ model: model }, 'Chief model: ' + modelWord(model) + '.');
  });
  if (els.chiefWorkerCap) {
    els.chiefWorkerCap.addEventListener('change', function () {
      const value = wholeNumberIn(els.chiefWorkerCap, 'Worker cap');
      if (value === null) return;
      putChiefSettings({ worker_cap: value }, 'Worker cap saved.', els.chiefWorkerCap);
    });
  }
  const scope = els.chiefWorkerCapScopeToggle;
  if (scope) {
    scope.addEventListener('click', function () {
      const chiefOnly = scope.getAttribute('aria-checked') !== 'true';
      setSwitch(scope, chiefOnly);
      putChiefSettings({ worker_cap_scope: chiefOnly ? 'chief' : 'all' },
        chiefOnly ? 'Worker cap counts chief sessions only.' : 'Worker cap counts every session.');
    });
  }
  wireChiefDoorbell();
  const toggle = els.chiefAutoCompactToggle;
  const field = els.chiefAutoCompactThreshold;
  if (!toggle || !field) return;
  // Off stores 0; on stores the percent in the field.
  toggle.addEventListener('click', async function () {
    const on = toggle.getAttribute('aria-checked') !== 'true';
    const value = on ? wholeNumberIn(field, 'Compact at context use') : 0;
    if (value === null) return;
    setFieldError(field, '');
    if (await patchConfig({ chief_auto_compact_threshold: value })) {
      toast(on ? 'Chief auto-compact on at ' + value + '%.' : 'Chief auto-compact off.', 'good');
    }
  });
  field.addEventListener('change', function () {
    if (toggle.getAttribute('aria-checked') !== 'true') return;
    const value = wholeNumberIn(field, 'Compact at context use');
    if (value === null) return;
    saveConfigField(field, { chief_auto_compact_threshold: value },
      'Chief auto-compact at ' + value + '%.');
  });
}

// The doorbell is read by its 3 s tick, the job-failure event by the next
// failed run.
function wireChiefDoorbell() {
  wireConfigSwitch(els.chiefDoorbellToggle, 'chief_doorbell', { label: 'Chief doorbell' });
  const fallback = els.chiefFallbackMinutes;
  if (fallback) {
    fallback.addEventListener('change', function () {
      const value = wholeNumberIn(fallback, 'Fallback check');
      if (value === null) return;
      saveConfigField(fallback, { chief_fallback_minutes: value },
        value ? 'Chief fallback check every ' + value + ' min.' : 'Chief fallback check off.');
    });
  }
  document.querySelectorAll('#chiefSheet [data-chief-timing]').forEach(function (field) {
    field.addEventListener('change', function () {
      const name = field.dataset.timingName;
      const value = wholeNumberIn(field, name);
      if (value === null) return;
      const patch = {};
      patch[field.dataset.chiefTiming] = value;
      saveConfigField(field, patch, name + ' set to ' + value + '.');
    });
  });
  wireConfigSwitch(els.chiefAutoDrainToggle, 'chief_auto_drain', { label: 'Auto-drain queue' });
  wireConfigSwitch(els.chiefJobFailuresToggle, 'chief_job_failures', { label: 'Chief job failures' });
  wireConfigSwitch(els.chiefJobSelfHealToggle, 'chief_job_self_heal', { label: 'Job self-heal' });
  wireConfigSwitch(els.chiefWeeklyProposalsToggle, 'chief_weekly_proposals', { label: 'Weekly proposals' });
}

// ------------------------------------------------------------------- wire
export function wireSettingsSheets() {
  document.querySelectorAll('#paneSettings [data-sheet]').forEach(function (btn) {
    btn.addEventListener('click', function () { showPath([btn.dataset.sheet]); });
  });
  document.querySelectorAll('dialog.settings-sheet').forEach(function (d) {
    d.querySelectorAll('[data-sheet-back]').forEach(function (b) {
      b.addEventListener('click', back);
    });
    d.querySelectorAll('[data-sheet-close]').forEach(function (b) {
      b.addEventListener('click', closeSheets);
    });
    // Escape steps back one level, like the header's back.
    d.addEventListener('cancel', function (e) {
      e.preventDefault();
      back();
    });
    // Closed some other way (a browser forcing a repeated Escape through):
    // nothing is left open, so forget the path. The event is queued, so by
    // the time it runs after this module's own close the path has moved on
    // and its top is another sheet, or nothing. It can also be stale for the
    // top sheet itself: Back reopens the parent, and the parent's earlier
    // close event may land after that. An open dialog was not closed,
    // or the path would be wiped under a sheet that is still showing.
    d.addEventListener('close', function () {
      if (d.open || stack[stack.length - 1] !== d.id) return;
      stack.length = 0;
      renderSettingsValues();
    });
  });
  wireFields();
  wireSpeech();
  wireServing();
  wireChief();
  wireMirrorBrowser();
  document.addEventListener('launcher:tab', function (e) {
    if (e.detail && e.detail.tab === 'settings') renderSettingsValues();
  });
}
