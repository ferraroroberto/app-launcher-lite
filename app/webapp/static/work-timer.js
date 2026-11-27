/* A button's ticking "working" label, apart from the voice code that also
 * uses it: the Board's dispatch buttons show it at first load, and the
 * dictation and composer code loads only when a composer opens.
 */

import { icon } from './_vendored/icons/icons.js';

// Tiny "working" indicator: swap a button's label for a ticking
// elapsed-seconds timer so a blind background wait — single-shot
// transcribe, streamed finish — visibly shows progress instead of looking
// stuck. ``workingLabel`` defaults to the hourglass glyph; pass a richer
// label for wide buttons. Labels are HTML (the Lucide icon() markup rides
// them). Returns a stop() that restores ``restoreHtml``.
export function startWorkTimer(btn, restoreHtml, workingLabel) {
  const lbl = workingLabel || icon('hourglass') + ' ';
  const t0 = Date.now();
  btn.classList.add('working');
  function tick() {
    const s = Math.floor((Date.now() - t0) / 1000);
    btn.innerHTML = lbl + s + 's';
  }
  tick();
  const id = setInterval(tick, 500);
  return function stop() {
    clearInterval(id);
    btn.classList.remove('working');
    btn.innerHTML = restoreHtml;
  };
}
