/* The Board's column sections as the page boots.
 *
 * The desktop grid is a kanban at a glance, so all five sections start open
 * there; the phone keeps the markup's default (the two live columns open).
 * Boot only, before the first tap: after that a section's open state is the
 * user's. board.js loads on the first Board visit (lazy-views.js), which is
 * too late for this, so main.js calls it with the page's own wiring.
 */

import { els } from '../state.js';

export const DESKTOP_BOARD = '(min-width: 700px) and (pointer: fine)';

const SECTIONS = ['boardColBacklog', 'boardColClaude', 'boardColYours', 'boardColOther', 'boardColDone'];

export function openDesktopColumns() {
  if (!window.matchMedia || !window.matchMedia(DESKTOP_BOARD).matches) return;
  SECTIONS.forEach(function (id) {
    const section = els[id];
    if (section) section.open = true;
  });
}
