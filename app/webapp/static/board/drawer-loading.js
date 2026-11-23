/* A Board card's drawer before its code has loaded.
 *
 * The drawer's code (drawer.js) loads with the first card opened, or once the
 * phone goes idle (lazy-views.js), so the lanes draw without it. A card
 * opened before then shows the drawer's own first state, its exchange being
 * read, then the drawer once the code lands; a failed load folds the card
 * again (the toast says why). The drawer's id lives here so the card header
 * can name it either way.
 */

import { state } from '../state.js';
import { loadBoardDrawer } from '../lazy-views.js';
import { renderBoard } from '../board.js';

// The open drawer's id, which its card header's aria-controls names.
export function drawerId(card) {
  return 'board-drawer-' + card.session_id;
}

let drawerAsked = false;

export function loadingDrawer(card) {
  const drawer = document.createElement('div');
  drawer.className = 'board-drawer board-drawer-loading';
  drawer.id = drawerId(card);
  const exchange = document.createElement('div');
  exchange.className = 'board-exchange';
  exchange.dataset.state = 'loading';
  exchange.setAttribute('role', 'status');
  exchange.setAttribute('aria-live', 'polite');
  exchange.textContent = 'Reading last exchange…';
  drawer.appendChild(exchange);
  if (!drawerAsked) {
    drawerAsked = true;
    loadBoardDrawer().then(function (ok) {
      drawerAsked = false;
      if (!ok) state.boardExpanded = null;
      renderBoard();
    });
  }
  return drawer;
}
