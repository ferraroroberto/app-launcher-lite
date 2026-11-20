/* The session overlay bar's model chip: names the session's model and, for a
 * Copilot session, opens the Switch model sheet. A pick goes to
 * POST /api/claude-code/sessions/<sid>/model, which types Copilot's own
 * /model <id> for that session only (the default in Settings stays) and waits
 * for Copilot's event log to report the new model. The chip changes model
 * only on that report, never optimistically: a switch the server could not
 * confirm leaves the old model showing and says why. The chip is hidden
 * whenever the session's model isn't known, never a guess.
 */

import { els, state } from '../state.js';
import { apiFailToast, sessionRoute, toast } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { bindLongPressHint } from '../long-press-hint.js';
import { icon } from '../_vendored/icons/icons.js';

// What a switch does, shown on the sheet (Copilot's own wording, probed live).
const SWITCH_NOTE = 'Copilot switches for this session only; the default model in Settings stays as it is.';

// The switch in flight, or the last one the server could not confirm:
// { sid, phase: 'switching' | 'unconfirmed', target, reason }.
let pending = null;

// The open session as the list knows it now (agent, kind, model), falling back
// to the object the overlay was opened with, as the context ring resolves it.
function viewSession() {
  const v = state.sessionView;
  if (!v) return null;
  const sid = v.session.session_id;
  return (state.sessions || []).find(function (x) { return x.session_id === sid; }) || v.session;
}

// Why this session's chip can't switch, or null when it can.
export function modelSwitchReason(s) {
  const agent = String((s && s.agent) || '').toLowerCase();
  if (agent !== 'copilot') return "This agent can't switch model inside a session";
  if (s.kind === 'remote') return 'Detached session: switch the model with /model in the PC console';
  return null;
}

function chipTitle(model, mine, reason) {
  if (mine && mine.phase === 'switching') return 'Switching to ' + mine.target + '…';
  if (mine) return 'Switch to ' + mine.target + ' not confirmed: ' + mine.reason;
  if (reason) return 'Model: ' + model + '. ' + reason;
  return 'Model: ' + model + '. Tap to switch';
}

// Paint the chip for `session`: its model, and whether a tap can switch it.
export function renderModelChip(session) {
  const chip = els.terminalModel;
  if (!chip) return;
  const model = String((session && session.model) || '').trim();
  const sid = session && session.session_id;
  if (pending && pending.sid === sid && pending.phase === 'unconfirmed' && model === pending.target) {
    pending = null; // Copilot reported it after all
  }
  const mine = pending && pending.sid === sid ? pending : null;
  (els.terminalModelLabel || chip).textContent = model;
  chip.hidden = !model;
  if (mine) chip.dataset.switch = mine.phase;
  else delete chip.dataset.switch;
  chip.setAttribute('aria-busy', String(!!mine && mine.phase === 'switching'));
  if (!model) {
    chip.removeAttribute('title');
    chip.removeAttribute('aria-label');
    return;
  }
  const reason = modelSwitchReason(session);
  if (reason) chip.setAttribute('aria-disabled', 'true');
  else chip.removeAttribute('aria-disabled');
  chip.title = chipTitle(model, mine, reason);
  chip.setAttribute('aria-label', chip.title);
}

// The models on the install's Copilot list, plus the one the session runs
// when the list no longer carries it, so the current row is always there.
function modelChoices(own) {
  const listed = ((state.config && state.config.copilot && state.config.copilot.models_available) || [])
    .map(String);
  return own && listed.indexOf(own) < 0 ? [own].concat(listed) : listed;
}

function pickRow(id, current, onPick) {
  const btn = document.createElement('button');
  btn.dataset.track = 'session.model-pick';
  btn.type = 'button';
  btn.className = 'settings-row model-switch-pick';
  btn.setAttribute('role', 'listitem');
  btn.dataset.value = id;
  const label = document.createElement('span');
  label.className = 'settings-row-label';
  label.textContent = id;
  btn.appendChild(label);
  if (id === current) {
    btn.setAttribute('aria-current', 'true');
    btn.setAttribute('aria-label', id + ', current model');
    const mark = document.createElement('span');
    mark.className = 'model-switch-current';
    mark.innerHTML = icon('circle-check');
    btn.appendChild(mark);
  }
  btn.addEventListener('click', function () { onPick(id); });
  return btn;
}

function openModelPicker(s) {
  const sheet = els.modelSwitchSheet;
  if (!sheet) return;
  const own = String(s.model || '');
  els.modelSwitchSheetNote.textContent = SWITCH_NOTE;
  els.modelSwitchList.replaceChildren.apply(
    els.modelSwitchList,
    modelChoices(own).map(function (id) {
      return pickRow(id, own, function (picked) {
        sheet.close();
        if (picked !== own) switchModel(s, picked);
      });
    }),
  );
  if (!sheet.open) sheet.showModal();
}

function repaintIfShowing(sid) {
  const s = viewSession();
  if (s && s.session_id === sid) renderModelChip(s);
}

async function switchModel(s, target) {
  const sid = s.session_id;
  pending = { sid: sid, phase: 'switching', target: target, reason: '' };
  repaintIfShowing(sid);
  toast('Switching to ' + target + '…');
  let res;
  try {
    res = await terminalJsonApi(sessionRoute(sid, '/model'), {
      method: 'POST',
      body: { model: target },
    });
  } catch (exc) {
    pending = { sid: sid, phase: 'unconfirmed', target: target,
      reason: (exc && exc.message) || 'the switch failed' };
    repaintIfShowing(sid);
    apiFailToast('Switch to ' + target + ' failed', exc);
    return;
  }
  if (res && res.outcome === 'switched') {
    pending = null;
    // The server's answer is Copilot's own report of the model.
    s.model = res.model;
    repaintIfShowing(sid);
    toast('Switched to ' + res.model, '', { icon: 'circle-check' });
    return;
  }
  const reason = (res && res.detail) || 'Copilot did not report it';
  pending = { sid: sid, phase: 'unconfirmed', target: target, reason: reason };
  repaintIfShowing(sid);
  toast('Switch to ' + target + ' not confirmed: ' + reason, 'error');
}

export function wireModelChip() {
  const chip = els.terminalModel;
  if (!chip) return;
  bindLongPressHint(chip);
  chip.addEventListener('click', function () {
    const s = viewSession();
    if (!s) return;
    const reason = modelSwitchReason(s);
    if (reason) {
      toast(reason, '', { icon: 'info' });
      return;
    }
    if (pending && pending.sid === s.session_id && pending.phase === 'switching') {
      toast('Switching to ' + pending.target + '…');
      return;
    }
    openModelPicker(s);
  });
  [els.modelSwitchSheetClose, els.modelSwitchSheetDone].forEach(function (b) {
    if (b) b.addEventListener('click', function () { els.modelSwitchSheet.close(); });
  });
  if (els.modelSwitchSheet) {
    els.modelSwitchSheet.addEventListener('click', function (ev) {
      if (ev.target === els.modelSwitchSheet) els.modelSwitchSheet.close();
    });
  }
}
