/* The session overlay bar's model chip: names the session's model, read-only.
 * Copilot takes its model at launch from Settings and has no in-session
 * switch the launcher drives, so a tap only says so. The chip is hidden
 * whenever the session's model isn't known, never a guess.
 */

import { els } from '../state.js';
import { toast } from '../api.js';
import { bindLongPressHint } from '../long-press-hint.js';

const FIXED_NOTE = 'The model is set in Settings; a session keeps the one it launched with';

// Paint the chip for `session`: its model, or hidden when unknown.
export function renderModelChip(session) {
  const chip = els.terminalModel;
  if (!chip) return;
  const model = String((session && session.model) || '').trim();
  (els.terminalModelLabel || chip).textContent = model;
  chip.hidden = !model;
  if (!model) {
    chip.removeAttribute('title');
    chip.removeAttribute('aria-label');
    return;
  }
  chip.setAttribute('aria-disabled', 'true');
  chip.title = 'Model: ' + model + '. ' + FIXED_NOTE;
  chip.setAttribute('aria-label', chip.title);
}

export function wireModelChip() {
  const chip = els.terminalModel;
  if (!chip) return;
  bindLongPressHint(chip);
  chip.addEventListener('click', function () {
    toast(FIXED_NOTE, '', { icon: 'info' });
  });
}
