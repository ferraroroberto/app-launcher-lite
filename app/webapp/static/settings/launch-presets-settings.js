/* Settings › GitHub Copilot › Launch presets.
 *
 * The editor for the named Copilot launches the Coding launch dropdown offers
 * (shared/launch-presets.js). Each saved preset is a row: its name, the model,
 * effort and context tier it launches with, and a kebab holding Edit and
 * Remove. Under the list, one form adds a preset; Edit loads a row into the
 * same form, which then saves the changes in place. A row's name is also
 * editable on the row itself and saved when the field loses focus.
 *
 * Every change posts the whole list to /api/config and the rows are redrawn
 * from the re-read config, so a refused entry never lingers. The models come
 * from the install's Copilot list, the efforts a model offers and the context
 * tiers from the same config block. A refusal names the field it is about and
 * shows beside that field.
 */

import { el } from '../dom-utils.js';
import { state } from '../state.js';
import { toast } from '../api.js';
import { icon } from '../_vendored/icons/icons.js';
import { ACTION_ROW_MENU } from '../action-rows.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';

// The most presets the server keeps (src/launching/presets.py MAX_PRESETS).
const MAX_PRESETS = 12;
const FORM_FIELDS = ['name', 'model', 'effort', 'context'];
const presetMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });
let save = null;
let editingId = '';
let modelsSignature = '';

function copilot() {
  return (state.config && state.config.copilot) || {};
}

function presetList() {
  return ((state.config && state.config.launch_presets) || []).map(function (p) {
    return Object.assign({}, p);
  });
}

function titleCase(value) {
  return value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, ' ');
}

// The levels a model offers: the server's list for it, every level the CLI
// takes for a model it has no row for (one dropped from the list since).
function effortsFor(model) {
  const byModel = copilot().preset_efforts || {};
  if (byModel[model]) return byModel[model];
  return (copilot().efforts_available || []).filter(Boolean);
}

function contextLabel(tier) {
  return tier ? titleCase(tier) : '';
}

function field(id) {
  return document.getElementById(id);
}

function showFieldError(name, message) {
  const note = field('launchPreset' + titleCase(name) + 'Error');
  const input = field('launchPreset' + titleCase(name));
  if (note) {
    note.textContent = message || '';
    note.hidden = !message;
  }
  if (input) {
    if (message) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
  }
}

function clearFormErrors() {
  FORM_FIELDS.forEach(function (name) { showFieldError(name, ''); });
}

// A refusal's message beside the control it names: the add form's field, or
// the row's own error line when the change was made on the row.
function showRefusal(refusal, rowId) {
  if (!refusal.message) return; // already toasted by the save
  const rowNote = rowId ? document.querySelector('[data-preset-error="' + rowId + '"]') : null;
  if (rowNote) {
    rowNote.textContent = refusal.message;
    rowNote.hidden = false;
    return;
  }
  const name = FORM_FIELDS.indexOf(refusal.field) !== -1 ? refusal.field : '';
  if (name) showFieldError(name, refusal.message);
  else toast(refusal.message, 'error');
}

function fillContextSelect(keep) {
  const select = field('launchPresetContext');
  if (!select) return;
  select.replaceChildren();
  const none = el('option', null, 'Saved setting');
  none.value = '';
  select.appendChild(none);
  (copilot().preset_contexts || []).forEach(function (tier) {
    const option = el('option', null, contextLabel(tier));
    option.value = tier;
    select.appendChild(option);
  });
  select.value = keep || '';
}

function fillEffortSelect() {
  const model = field('launchPresetModel');
  const select = field('launchPresetEffort');
  if (!model || !select) return;
  const efforts = model.value ? effortsFor(model.value) : [];
  const keep = select.value;
  select.replaceChildren();
  efforts.forEach(function (effort) {
    const option = el('option', null, titleCase(effort));
    option.value = effort;
    select.appendChild(option);
  });
  if (efforts.length) {
    select.value = efforts.indexOf(keep) !== -1 ? keep : (efforts.indexOf('medium') !== -1 ? 'medium' : efforts[0]);
  }
}

// Refilled only when the lists behind the selects change, so a config re-read
// while the owner is choosing never resets their pick.
function fillModelSelect() {
  const select = field('launchPresetModel');
  if (!select) return;
  const models = offeredModels();
  const efforts = copilot().preset_efforts || {};
  const signature = JSON.stringify([models, efforts, copilot().preset_contexts || []]);
  if (signature === modelsSignature) return;
  modelsSignature = signature;
  const keep = select.value;
  const keepContext = field('launchPresetContext') ? field('launchPresetContext').value : '';
  select.replaceChildren();
  models.forEach(function (model) {
    const option = el('option', null, model);
    option.value = model;
    select.appendChild(option);
  });
  if (models.indexOf(keep) !== -1) select.value = keep;
  fillEffortSelect();
  fillContextSelect(keepContext);
}

// The Copilot list, plus the model of the preset being edited when the list no
// longer has it, so editing such a preset keeps its model selectable.
function offeredModels() {
  const models = (copilot().models_available || []).slice();
  const editing = presetList().find(function (p) { return p.id === editingId; });
  if (editing && models.indexOf(editing.model) === -1) models.push(editing.model);
  return models;
}

function listed(model) {
  return (copilot().models_available || []).indexOf(model) !== -1;
}

function metaText(preset) {
  const parts = [preset.model, titleCase(preset.effort)];
  if (preset.context) parts.push(contextLabel(preset.context));
  if (!listed(preset.model)) parts.push('model not on your list');
  return parts.join(' · ');
}

// Put `preset` in the form for editing; null returns the form to adding.
function loadForm(preset) {
  editingId = preset ? preset.id : '';
  clearFormErrors();
  fillModelSelect();
  const name = field('launchPresetName');
  const model = field('launchPresetModel');
  if (!name || !model) return;
  name.value = preset ? preset.name : '';
  if (preset) {
    model.value = preset.model;
    fillEffortSelect();
    field('launchPresetEffort').value = preset.effort;
    field('launchPresetContext').value = preset.context || '';
    name.focus();
  }
  renderFormMode();
}

function renderFormMode() {
  const editing = !!editingId;
  const add = field('launchPresetAdd');
  const cancel = field('launchPresetCancel');
  const title = field('launchPresetFormTitle');
  if (add) {
    add.textContent = editing ? 'Save preset' : 'Add preset';
    add.disabled = !editing && presetList().length >= MAX_PRESETS;
  }
  if (cancel) cancel.hidden = !editing;
  if (title) title.textContent = editing ? 'Edit preset' : 'New preset';
}

function renderRow(preset, all) {
  const li = el('li', 'launch-preset-row');
  const text = el('div', 'launch-preset-text');
  const name = el('input', 'input-native launch-preset-name');
  name.type = 'text';
  name.value = preset.name;
  name.maxLength = 40;
  name.setAttribute('aria-label', 'Preset name');
  name.autocomplete = 'off';
  name.addEventListener('change', async function () {
    const next = name.value.trim();
    if (!next || next === preset.name) {
      name.value = preset.name;
      return;
    }
    const refusal = await save(
      all.map(function (p) { return p.id === preset.id ? Object.assign({}, p, { name: next }) : p; }),
      'Preset renamed.'
    );
    if (refusal) {
      name.value = preset.name;
      showRefusal(refusal, preset.id);
    }
  });
  const error = el('p', 'field-error launch-preset-error');
  error.dataset.presetError = preset.id;
  error.setAttribute('role', 'alert');
  error.hidden = true;
  text.append(name, el('span', 'launch-preset-meta', metaText(preset)), error);
  const kebab = el('button', 'icon-button action-row-kebab');
  kebab.dataset.track = 'launch-presets.menu';
  kebab.type = 'button';
  kebab.title = 'Preset actions';
  kebab.setAttribute('aria-label', 'Actions for ' + preset.name);
  kebab.innerHTML = icon('ellipsis-vertical');
  presetMenu.attach(preset.id, kebab, [{
    dataset: { item: 'launch-preset-edit-item' }, glyph: 'pencil',
    label: 'Edit', ariaLabel: 'Edit ' + preset.name,
    onTap: function () { loadForm(preset); },
  }, {
    dataset: { item: 'launch-preset-remove-item' }, glyph: 'trash-2', danger: true,
    label: 'Remove', ariaLabel: 'Remove ' + preset.name,
    onTap: async function () {
      const refusal = await save(
        all.filter(function (p) { return p.id !== preset.id; }), 'Preset removed.'
      );
      if (refusal) showRefusal(refusal, preset.id);
      else if (editingId === preset.id) loadForm(null);
    },
  }]).dataset.surface = 'settings.preset-menu';
  li.append(text, kebab);
  return li;
}

// Redraw the list and the form's selects from the loaded config.
export function renderLaunchPresetsEditor() {
  const list = field('launchPresetList');
  if (!list) return;
  const all = presetList();
  list.replaceChildren();
  all.forEach(function (preset) { list.appendChild(renderRow(preset, all)); });
  presetMenu.endRender();
  fillModelSelect();
  if (editingId && !all.some(function (p) { return p.id === editingId; })) editingId = '';
  renderFormMode();
}

// `savePresets(list)` resolves null once the list is saved and the config
// re-read, or `{ message, field }` for a refusal to show beside its field.
export function wireLaunchPresets(savePresets) {
  save = async function (presets, message) {
    const refusal = await savePresets(presets);
    if (!refusal) toast(message, 'good');
    return refusal;
  };
  const add = field('launchPresetAdd');
  const name = field('launchPresetName');
  const model = field('launchPresetModel');
  if (!add || !name || !model) return;
  model.addEventListener('change', function () {
    fillEffortSelect();
    showFieldError('model', '');
    showFieldError('effort', '');
  });
  name.addEventListener('input', function () { showFieldError('name', ''); });
  field('launchPresetEffort').addEventListener('change', function () { showFieldError('effort', ''); });
  field('launchPresetContext').addEventListener('change', function () { showFieldError('context', ''); });
  const cancel = field('launchPresetCancel');
  if (cancel) cancel.addEventListener('click', function () { loadForm(null); });

  async function submit() {
    clearFormErrors();
    const label = name.value.trim();
    if (!label) {
      showFieldError('name', 'Give the preset a name.');
      name.focus();
      return;
    }
    if (!model.value) {
      showFieldError('model', 'Add a model to the Copilot list first.');
      return;
    }
    const entry = {
      name: label, agent: 'copilot', model: model.value, effort: field('launchPresetEffort').value,
    };
    const context = field('launchPresetContext').value;
    if (context) entry.context = context;
    const all = presetList();
    const editing = editingId;
    const next = editing
      ? all.map(function (p) { return p.id === editing ? Object.assign({ id: editing }, entry) : p; })
      : all.concat(entry);
    const refusal = await save(next, editing ? 'Preset saved.' : 'Preset added.');
    if (refusal) {
      showRefusal(refusal, '');
      return;
    }
    loadForm(null);
  }
  add.addEventListener('click', submit);
  name.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  });
}
