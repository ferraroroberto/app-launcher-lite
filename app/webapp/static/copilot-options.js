/* Coding options card: a collapsible panel (collapsed by default) with the
 * GitHub Copilot subsection — model / context / effort pickers plus the
 * autopilot and skip-permissions toggles and a flags preview.
 *
 * `patchConfig` round-trips through GET /api/config so the SPA's view of
 * config stays a single source of truth — server-computed flags + the
 * `models_available` / `efforts_available` enums included.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi } from './api.js';
import { toggleAriaChecked } from './dom-utils.js';
import { setSwitch } from './_vendored/switch/switch.js';

export async function fetchConfig() {
  const body = await jsonApi('/api/config');
  state.config = body;
  els.projectsDir.value = body.projects_dir || '';
  els.projectsIgnore.value = (body.projects_ignore || []).join('\n');
  els.appsScanRoot.value = body.apps_scan_root || '';
  if (els.teamOsDir) els.teamOsDir.value = body.team_os_dir || '';
  if (els.terminalHistoryLines) {
    if (body.terminal_history_lines_min != null) {
      els.terminalHistoryLines.min = body.terminal_history_lines_min;
    }
    if (body.terminal_history_lines_max != null) {
      els.terminalHistoryLines.max = body.terminal_history_lines_max;
    }
    els.terminalHistoryLines.value = body.terminal_history_lines || '';
  }
  if (els.bootAutostartToggle) {
    setSwitch(els.bootAutostartToggle, !!body.boot_autostart_enabled);
  }
  renderCopilotOptions();
}

// Rebuild a native <select> from a list of {value, label} pairs and set the
// current value — the Copilot subsection has three of these (model, context,
// effort), all fed by server enums.
function fillSelect(sel, options, current) {
  sel.innerHTML = '';
  options.forEach(function (o) {
    const opt = document.createElement('option');
    opt.value = o.value;
    opt.textContent = o.label;
    sel.appendChild(opt);
  });
  sel.value = current || '';
}

export function renderCopilotOptions() {
  const c = state.config && state.config.copilot;
  if (!c) return;
  // Model picker — a <select> fed by the config-driven copilot_models list
  // (read-only from the UI; edited in webapp_config.json). The empty-value
  // "Default (auto)" option launches without --model — Copilot picks.
  fillSelect(
    els.copilotModel,
    [{ value: '', label: 'Default (auto)' }].concat(
      (c.models_available || []).map(function (m) {
        return { value: m, label: m };
      })
    ),
    c.model
  );
  // Context window (--context; '' omits the flag).
  fillSelect(
    els.copilotContext,
    (c.contexts_available || []).map(function (v) {
      return { value: v, label: v || 'Default (omit)' };
    }),
    c.context
  );
  // Reasoning effort (--effort; '' omits it, and the server only ever
  // emits it alongside an explicit model — the auto model rejects it).
  fillSelect(
    els.copilotEffort,
    (c.efforts_available || []).map(function (v) {
      return { value: v, label: v || 'Default (omit)' };
    }),
    c.effort
  );
  setSwitch(els.copilotAutopilot, !!c.autopilot);
  setSwitch(els.copilotSkipPerms, !!c.skip_permissions);
  els.copilotFlagsPreview.textContent =
    'copilot' + (c.computed_flags ? ' ' + c.computed_flags : '');
}

export async function patchConfig(patch) {
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    await fetchConfig();
  } catch (exc) {
    apiFailToast('Save failed', exc);
  }
}

// role="switch" buttons (issue #355): click reads the current aria-checked,
// flips it, applies it optimistically, then patchConfig() round-trips
// through GET /api/config, which re-renders from server truth anyway.
function wireBoolSwitch(el, patchKey) {
  el.addEventListener('click', function () {
    const next = el.getAttribute('aria-checked') !== 'true';
    setSwitch(el, next);
    patchConfig({ [patchKey]: next });
  });
}

export function wireCopilotOptions() {
  wireBoolSwitch(els.copilotSkipPerms, 'copilot_skip_permissions');
  wireBoolSwitch(els.copilotAutopilot, 'copilot_autopilot');
  els.copilotModel.addEventListener('change', function () {
    patchConfig({ copilot_model: els.copilotModel.value });
  });
  els.copilotContext.addEventListener('change', function () {
    patchConfig({ copilot_context: els.copilotContext.value });
  });
  els.copilotEffort.addEventListener('change', function () {
    patchConfig({ copilot_effort: els.copilotEffort.value });
  });
  // The ☁️ Detached and ↺ Resume toggles are plain client-side switches
  // (no server config — read at session-launch time in apps.js). They live
  // in the Projects card's <summary> (#496 — the launch surface) so they
  // stay visible when the panel is collapsed — but a click there would
  // also expand/collapse the <details>, so stopPropagation lives alongside
  // the flip.
  [els.codingDetached, els.codingResume].forEach(function (btn) {
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      toggleAriaChecked(btn);
    });
  });
}
