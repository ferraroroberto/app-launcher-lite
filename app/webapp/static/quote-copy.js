/* The copy button on a rendered Markdown blockquote or fenced code
block, and on the Board's owner-step command block, which borrows the fence's
frame (board/owner-checks.js). A button marked `data-fill-root` is such an
owner step: Copy alone fills its `<root>` with this PC's fleet root, and says
when it could not.

`markdown.js` emits the button with the quote's (or block's) plain text in
`data-copy`,
but stays DOM- and toast-free so it runs under plain Node. The click lives
here instead: one delegated listener on the document, so every consumer of
the renderer (Chat pane, plan cards) gets a working button
without each wiring its own.

The clipboard write happens synchronously inside the tap, the same iOS rule
`transcript-render.js`'s turn copy follows: an `await` ahead of it loses the
gesture and the copy fails silently on the phone.
*/

import { toast, writeClipboard } from './api.js';
import { fillRoot } from './board/fill-root.js';
import { state } from './state.js';

export function wireQuoteCopy() {
  document.addEventListener('click', function (ev) {
    const btn = ev.target.closest && ev.target.closest('.md-quote-copy, .md-block-copy');
    if (!btn) return;
    const what = btn.dataset.what || (btn.classList.contains('md-block-copy') ? 'Code' : 'Quote');
    ev.preventDefault();
    // Inside a modal sheet the page's toast sits under the dialog and the
    // rest of the page is inert: use the sheet's own toast when it has one.
    const dialog = btn.closest('dialog');
    const host = (dialog && dialog.querySelector('.sheet-toast')) || undefined;
    const fail = function () { toast('Clipboard unavailable — copy manually', 'error', { host: host }); };
    const raw = btn.getAttribute('data-copy') || '';
    const filled = 'fillRoot' in btn.dataset
      ? fillRoot(raw, state.board && state.board.fleet_root)
      : { text: raw, unfilled: false };
    const opts = { icon: 'copy', host: host };
    let done = what + ' copied';
    if (filled.unfilled) {
      done += ' with <root> unfilled: fleet folder unknown';
      // It will not run as pasted, so it stays long enough to read.
      opts.ms = 4500;
    }
    writeClipboard(filled.text, { host: host }).then(function () { toast(done, 'good', opts); }, fail);
  });
}
