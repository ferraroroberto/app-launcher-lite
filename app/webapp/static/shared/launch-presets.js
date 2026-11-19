/* Launch presets in the Coding launch toolbar.
 *
 * A preset is a named Copilot model + effort (and optionally a context tier)
 * saved in the config's `launch_presets`. The toolbar gets one dropdown:
 * "Default" (the saved Copilot settings, nothing extra sent) and one row per
 * preset. The pick is remembered on this device; a launch reads it through
 * activeLaunchPreset() and sends the preset's model, effort and context.
 *
 * Nothing is drawn while no preset is offered, so a toolbar without presets
 * looks as it did before. A preset whose model is not in the install's
 * Copilot model list is not offered (the server would refuse the launch);
 * it comes back when the model does.
 */

import { el } from '../dom-utils.js';
import { wireModelCombo } from './model-combo.js';

const DEFAULT_VALUE = 'default';

// surface key -> { slot, picker, controller, offered, activeId, signature }
const surfaces = new Map();

function storageKey(key) {
  return 'app-launcher-lite.launch-preset.' + key;
}

function readActive(key) {
  try { return window.localStorage.getItem(storageKey(key)) || ''; } catch (_e) { return ''; }
}

function writeActive(key, id) {
  try {
    if (id) window.localStorage.setItem(storageKey(key), id);
    else window.localStorage.removeItem(storageKey(key));
  } catch (_e) { /* storage blocked: the pick lasts for this page only */ }
}

function offeredPresets(config) {
  const listed = ((config && config.copilot) || {}).models_available || [];
  return ((config && config.launch_presets) || []).filter(function (preset) {
    return preset.agent === 'copilot' && listed.indexOf(preset.model) !== -1;
  });
}

function presetLabel(preset) {
  return preset.name + ' · ' + preset.effort + (preset.context ? ' · ' + preset.context : '');
}

function pickerMarkup(key) {
  const root = el('span', 'model-combo launch-preset-combo');
  root.id = key + 'PresetCombo';
  root.dataset.value = DEFAULT_VALUE;
  const trigger = el('button', 'model-combo-trigger', 'Default');
  trigger.type = 'button';
  trigger.id = key + 'PresetBtn';
  trigger.dataset.track = 'launch-preset.combo';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', key + 'PresetMenu');
  trigger.setAttribute('aria-label', 'Launch preset');
  trigger.title = 'Launch preset: a model with the effort it runs at';
  const menu = el('span', 'model-combo-menu');
  menu.id = key + 'PresetMenu';
  menu.dataset.surface = 'code.preset-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', 'Launch preset');
  menu.hidden = true;
  root.append(trigger, menu);
  return root;
}

function stateFor(key) {
  let state = surfaces.get(key);
  if (!state) {
    state = { slot: null, picker: null, controller: null, offered: [], activeId: readActive(key), signature: '' };
    surfaces.set(key, state);
  }
  return state;
}

function removePicker(state) {
  if (!state.picker) return;
  state.controller.destroy();
  state.picker.remove();
  state.picker = null;
  state.controller = null;
  state.signature = '';
  if (state.slot) state.slot.hidden = true;
}

function ensurePicker(key, state) {
  if (state.picker || !state.slot) return;
  state.picker = pickerMarkup(key);
  state.slot.appendChild(state.picker);
  state.slot.hidden = false;
  state.controller = wireModelCombo(state.picker, function (value) {
    const preset = state.offered.find(function (p) { return p.id === value; });
    state.activeId = preset ? preset.id : '';
    writeActive(key, state.activeId);
  });
}

// Hand a surface the element its dropdown is drawn into (hidden until a
// preset is offered).
export function mountLaunchPresets(key, slot) {
  const state = stateFor(key);
  state.slot = slot;
  slot.hidden = true;
}

// The preset in force on a surface, or null for Default.
export function activeLaunchPreset(key) {
  const state = surfaces.get(key);
  if (!state) return null;
  return state.offered.find(function (p) { return p.id === state.activeId; }) || null;
}

// Redraw every mounted dropdown from `config`. Called whenever the config lands.
export function syncLaunchPresets(config) {
  surfaces.forEach(function (state, key) {
    state.offered = offeredPresets(config);
    if (!state.offered.length) {
      removePicker(state);
      return;
    }
    ensurePicker(key, state);
    if (!state.picker) return;
    const options = [{ value: DEFAULT_VALUE, label: 'Default' }].concat(
      state.offered.map(function (p) { return { value: p.id, label: presetLabel(p) }; })
    );
    // Rebuilt only when the list changed: setOptions closes an open menu, and
    // the config is re-read after every save.
    const signature = JSON.stringify(options);
    if (signature !== state.signature) {
      state.controller.setOptions(options);
      state.signature = signature;
    }
    if (!activeLaunchPreset(key)) state.activeId = '';
    state.controller.setValue(state.activeId || DEFAULT_VALUE);
  });
}
