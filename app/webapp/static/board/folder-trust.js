/* A session row waiting on Copilot's "Confirm folder trust" prompt: the card
 * carries `folder_trust` (the folder's name, "" otherwise). Copilot reads
 * nothing until the prompt is answered, so the row says so by name and offers
 * the one tap that answers it with "Yes, and remember this folder". The server
 * reads the screen again before typing, so a stale card never types blind.
 */

import { apiFailToast, jsonApi, sessionRoute, toast } from '../api.js';
import { chip } from '../glance.js';
import { icon } from '../_vendored/icons/icons.js';

async function trustFolder(card, btn, refresh) {
  btn.disabled = true;
  try {
    const res = await jsonApi(sessionRoute(card.session_id, '/folder-trust'), { method: 'POST' });
    toast(
      res.trusted ? 'Folder trusted.' : 'Sent, but the prompt is still showing.',
      res.trusted ? 'good' : ''
    );
    refresh();
  } catch (exc) {
    apiFailToast('Could not trust the folder', exc);
  }
  btn.disabled = false;
}

// `{ chip, button }` for a card on the prompt, `null` for any other card.
// `refresh` re-reads the Board once the tap has been answered.
export function folderTrustControls(card, refresh) {
  if (!card.folder_trust) return null;
  const button = document.createElement('button');
  button.dataset.track = 'board.folder-trust';
  button.type = 'button';
  button.className = 'icon-button action-row-verb board-folder-trust';
  button.innerHTML = icon('circle-check');
  button.title = 'Trust ' + card.folder_trust + ' (Copilot remembers it)';
  button.setAttribute('aria-label', 'Trust this folder');
  button.addEventListener('click', function () { trustFolder(card, button, refresh); });
  return { chip: chip('needs folder trust', 'attention', 'board-folder-trust-chip'), button: button };
}
