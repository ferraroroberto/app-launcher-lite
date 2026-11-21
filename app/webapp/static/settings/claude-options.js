/* The coding agent (Settings › Coding agent): GitHub Copilot CLI, this
 * fork's only one — its model, reasoning effort and context window pickers,
 * the autopilot and skip-permissions switches, the list of model ids every
 * picker offers, and the server's own flags line, read-only.
 *
 * The file keeps the full app's name so upstream syncs stay mechanical; it
 * also owns the config round trip every Settings field uses: `patchConfig`
 * POSTs a change, then re-reads GET /api/config so the SPA's view of config
 * stays a single source of truth (the server-computed flags and the
 * `models_available` / `efforts_available` / `contexts_available` lists
 * included).
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { setTeamOsModelOptions } from '../team-os.js';
import { syncLaunchPresets } from '../shared/launch-presets.js';
import { renderLaunchPresetsEditor, wireLaunchPresets } from './launch-presets-settings.js';
import { setSwitch } from '../_vendored/switch/switch.js';
import { renderSettingsValues } from './settings-sheets.js';
import { renderSpeech } from './speech-settings.js';
import { renderLidKeepAwake } from './lid-keep-awake.js';
import { renderNetworkAccess, wireNetworkAccess } from './network-access.js';

export async function fetchConfig(shouldApply) {
  const body = await jsonApi('/api/config');
  // A caller may own only one optimistic selection generation. Check after
  // the await, immediately before mutating shared state and repainting.
  if (shouldApply && !shouldApply()) return false;
  state.config = body;
  setFieldValue(els.projectsDir, body.projects_dir || '');
  setFieldValue(els.projectsIgnore, (body.projects_ignore || []).join('\n'));
  setFieldValue(els.appsScanRoot, body.apps_scan_root || '');
  setFieldValue(els.teamOsDir, body.team_os_dir || '');
  setFieldValue(els.fleetConfigDir, body.fleet_config_dir || '');
  setFieldValue(els.gitlabGroup, body.gitlab_group || '');
  setFieldValue(els.gitlabHost, body.gitlab_host || '');
  renderSpeech(body);
  if (els.terminalHistoryLines) {
    if (body.terminal_history_lines_min != null) {
      els.terminalHistoryLines.min = body.terminal_history_lines_min;
    }
    if (body.terminal_history_lines_max != null) {
      els.terminalHistoryLines.max = body.terminal_history_lines_max;
    }
    setFieldValue(els.terminalHistoryLines, body.terminal_history_lines || '');
  }
  if (els.largeUploadMaxMb) {
    if (body.large_upload_max_mb_min != null) els.largeUploadMaxMb.min = body.large_upload_max_mb_min;
    if (body.large_upload_max_mb_max != null) els.largeUploadMaxMb.max = body.large_upload_max_mb_max;
    setFieldValue(els.largeUploadMaxMb, body.large_upload_max_mb || '');
  }
  if (els.bootAutostartToggle) {
    setSwitch(els.bootAutostartToggle, !!body.boot_autostart_enabled);
  }
  renderLidKeepAwake(body);
  renderChiefAutoCompact(body);
  if (els.chiefDoorbellToggle) {
    setSwitch(els.chiefDoorbellToggle, body.chief_doorbell === true);
  }
  if (els.chiefAutoDrainToggle) {
    setSwitch(els.chiefAutoDrainToggle, body.chief_auto_drain !== false);
  }
  if (els.chiefJobFailuresToggle) {
    setSwitch(els.chiefJobFailuresToggle, body.chief_job_failures !== false);
  }
  if (els.chiefJobSelfHealToggle) {
    setSwitch(els.chiefJobSelfHealToggle, body.chief_job_self_heal === true);
  }
  if (els.chiefWeeklyProposalsToggle) {
    setSwitch(els.chiefWeeklyProposalsToggle, body.chief_weekly_proposals === true);
  }
  if (els.openSessionsInWindowToggle) {
    setSwitch(els.openSessionsInWindowToggle, body.open_sessions_in_window === true);
  }
  if (els.openSessionsInChatToggle) {
    setSwitch(els.openSessionsInChatToggle, body.open_sessions_in_chat !== false);
  }
  renderNetworkAccess(body);
  renderClaudeOptions();
  renderSettingsValues();
  return true;
}

// Settings fields save as they change, and every save re-reads the
// config. Leave the field being typed in alone: the user has moved on to it,
// and the stored value would overwrite what they typed so far.
function setFieldValue(el, value) {
  if (el && el !== document.activeElement) el.value = value;
}

// Settings › Chief's auto-compact: 0 is off, shown as the switch off and the percent
// field disabled at the default, since 0 keeps no earlier percent.
const _DEFAULT_CHIEF_AUTO_COMPACT = 30;

function renderChiefAutoCompact(body) {
  if (!els.chiefAutoCompactToggle) return;
  const field = els.chiefAutoCompactThreshold;
  const value = Number(body.chief_auto_compact_threshold) || 0;
  if (body.chief_auto_compact_threshold_min != null) field.min = body.chief_auto_compact_threshold_min;
  if (body.chief_auto_compact_threshold_max != null) field.max = body.chief_auto_compact_threshold_max;
  setSwitch(els.chiefAutoCompactToggle, value > 0);
  setFieldValue(field, String(value > 0 ? value : _DEFAULT_CHIEF_AUTO_COMPACT));
  field.disabled = value === 0;
}

// Rebuild a native <select> from {value, label} pairs and set its value.
// A stored value the list doesn't offer (a model dropped from the list since)
// still shows, never a silent swap to the first option. onchange is assigned,
// not added, because every config readback re-renders the same element.
export function fillSelect(select, options, current, onSelect) {
  if (!select) return;
  const items = options.slice();
  if (current && !items.some(function (o) { return o.value === current; })) {
    items.push({ value: current, label: current });
  }
  select.innerHTML = '';
  items.forEach(function (o) {
    const opt = document.createElement('option');
    opt.value = o.value;
    opt.textContent = o.label;
    select.appendChild(opt);
  });
  select.value = current || '';
  select.onchange = function () { onSelect(select.value); };
}

// "" is the server's "omit the flag": Copilot's own default.
export function choice(value) {
  return { value: value, label: value ? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, ' ') : 'Default' };
}

// The Coding agent sheet, from the config's `copilot` block.
// What Copilot's own settings file says, which Lite rewrites at each launch.
// A live session's state is only shown by `/remote show` inside it.
function remoteStateText(c) {
  const cli = c.remote_cli_state;
  const now = cli === true ? 'on' : cli === false ? 'off' : 'not readable';
  return 'Remote session shares each new Copilot session with Copilot\'s remote view and is applied at launch. '
    + 'Copilot\'s settings are ' + now + ' now; type /remote show in a session to see its own state.';
}

export function renderClaudeOptions() {
  setTeamOsModelOptions();
  syncLaunchPresets(state.config);
  const c = state.config && state.config.copilot;
  if (!c) return;
  fillSelect(
    els.copilotModel,
    [{ value: '', label: 'Default' }].concat((c.models_available || []).map(function (m) {
      return { value: m, label: m };
    })),
    c.model,
    function (v) { patchConfig({ copilot_model: v }); }
  );
  fillSelect(els.copilotEffort, (c.efforts_available || []).map(choice), c.effort,
    function (v) { patchConfig({ copilot_effort: v }); });
  // The default model refuses --effort, so the server sends it only with an
  // explicit model; the picker says so by standing down.
  if (els.copilotEffort) els.copilotEffort.disabled = !c.model;
  fillSelect(els.copilotContext, (c.contexts_available || []).map(choice), c.context,
    function (v) { patchConfig({ copilot_context: v }); });
  setSwitch(els.copilotAutopilot, !!c.autopilot);
  setSwitch(els.copilotSkipPerms, !!c.skip_permissions);
  setSwitch(els.copilotRemote, !!c.remote_session);
  if (els.copilotRemoteState) els.copilotRemoteState.textContent = remoteStateText(c);
  setFieldValue(els.copilotModels, (c.models_available || []).join('\n'));
  // A saved list replaces the code default, so a model added to the default
  // later is offered here instead of needing a JSON edit.
  const missing = c.models_missing_defaults || [];
  if (els.copilotModelsNew) els.copilotModelsNew.hidden = !missing.length;
  if (els.copilotModelsNewText) {
    els.copilotModelsNewText.textContent = 'New default models not in your list: ' + missing.join(', ') + '.';
  }
  if (els.copilotFlagsPreview) {
    els.copilotFlagsPreview.textContent = 'copilot' + (c.computed_flags ? ' ' + c.computed_flags : '');
  }
  renderLaunchPresetsEditor();
}

async function postConfigPatch(patch) {
  await jsonApi('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

// Resolves true on a saved-and-refreshed patch, false on a failed one (the
// toast already fired) — callers that only self-correct on failure branch
// on this.
export async function patchConfig(patch) {
  try {
    await postConfigPatch(patch);
    await fetchConfig();
    return true;
  } catch (exc) {
    apiFailToast('Save failed', exc);
    return false;
  }
}

// Saves the launch preset list. Resolves null once saved and the config is
// re-read, or { message, field } for a refusal the editor shows beside the
// field it names (the server's 400 carries `field`); any other failure toasts.
async function savePresets(presets) {
  try {
    await postConfigPatch({ launch_presets: presets });
  } catch (exc) {
    if (exc && exc.status === 400) {
      return { message: exc.message, field: (exc.body && exc.body.field) || '' };
    }
    apiFailToast('Save failed', exc);
    return { message: '', field: '' };
  }
  await fetchConfig();
  return null;
}

// role="switch" buttons: click reads the current aria-checked, flips it
// optimistically, then patchConfig() round-trips through GET /api/config,
// which re-renders from server truth. A failed save puts the switch back
// (patchConfig already fired the failure toast). A config switch is saved at
// once and re-read by the server on its next use, so off is also the phone's
// kill switch.
//   opts.label  toasts "<label> on." / "<label> off." once saved
//   opts.value  (next) => the value to save, when it is not `next` itself
//   opts.after  (next) => runs once saved
export function wireConfigSwitch(el, key, opts) {
  if (!el) return;
  const o = opts || {};
  el.addEventListener('click', async function () {
    const next = el.getAttribute('aria-checked') !== 'true';
    setSwitch(el, next);
    if (await patchConfig({ [key]: o.value ? o.value(next) : next })) {
      if (o.label) toast(o.label + (next ? ' on.' : ' off.'), 'good');
      if (o.after) o.after(next);
    } else {
      setSwitch(el, !next);
    }
  });
}

// The model list saves when the field is left; an empty list is refused here
// as the server would, with the stored list put back.
function wireModelList() {
  const field = els.copilotModels;
  if (!field) return;
  field.addEventListener('change', async function () {
    const models = field.value.split('\n')
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    if (!models.length) {
      toast('Keep at least one model id.', 'error');
      renderClaudeOptions();
      return;
    }
    if (await patchConfig({ copilot_models: models })) toast('Copilot models saved.', 'good');
  });
}

// Appends the default ids the saved list lacks, keeping the saved order and
// any custom ids.
function wireAddDefaultModels() {
  const btn = els.copilotAddDefaultModels;
  if (!btn) return;
  btn.addEventListener('click', async function () {
    const c = state.config && state.config.copilot;
    if (!c) return;
    const models = (c.models_available || []).concat(c.models_missing_defaults || []);
    if (await patchConfig({ copilot_models: models })) toast('Default models added.', 'good');
  });
}

export function wireClaudeOptions() {
  wireConfigSwitch(els.openSessionsInWindowToggle, 'open_sessions_in_window');
  wireConfigSwitch(els.openSessionsInChatToggle, 'open_sessions_in_chat');
  wireConfigSwitch(els.copilotAutopilot, 'copilot_autopilot', { label: 'Autopilot' });
  wireConfigSwitch(els.copilotSkipPerms, 'copilot_skip_permissions', { label: 'Skip permission prompts' });
  wireConfigSwitch(els.copilotRemote, 'copilot_remote_session', { label: 'Remote session' });
  wireNetworkAccess(fetchConfig);
  wireModelList();
  wireAddDefaultModels();
  wireLaunchPresets(savePresets);
}
