/* Compose bar for the live terminal (issue #315 split off terminal.js):
 * the predictive-text textarea behind the ➤ Send button.
 *
 * Compose bar (issue #37): a normal <textarea> with default predictive/
 * autocorrect/spellcheck so iOS/Android keyboards offer suggestions —
 * which they can't inside xterm's per-keystroke-wiped helper textarea.
 * ➤ Send forwards the buffered text, then a submitting \r as a SEPARATE
 * WS frame (see sendSubmit / #166).
 */

import { els, state } from './state.js';

// Max visible rows before the textarea scrolls internally. Roomy enough
// for a long dictated voice note (#165) without the bar eating the whole
// screen when the keyboard is up. The CSS min-height floors it at 2 rows.
const _COMPOSE_MAX_ROWS = 8;

// Delay (ms) before the submitting CR when the compose payload carries a
// pasted image path (issue #450). Gives Claude Code's path→attachment
// conversion time to finish so the CR isn't absorbed by it. On-device tunable:
// too small and the first tap still needs a second Enter; larger only adds a
// little submit latency on image sends, so this errs generous.
const _IMAGE_SUBMIT_DELAY_MS = 350;

// Bulk-text CR settle watch (issue #499): a dictation-sized paste can outrun
// Claude Code's bracketed-paste ingest when the machine is under load
// (concurrent PTY sessions, gates, browsers — #493 measured 5× latency
// spikes), so the immediately-following CR lands mid-ingest and becomes a
// newline into the still-settling composer instead of Submit. For payloads
// past the threshold, the CR is held until the session's output stream shows
// the paste was ingested AND settled: some output arrived after the send
// (Claude echoes the paste / collapses it into a "[Pasted text #N]" chip)
// and that output has been quiet for _BULK_QUIET_MS — with a floor (never
// sooner than _BULK_FLOOR_MS) and a cap (send anyway at _BULK_CAP_MS).
// Calibrated with a real-Claude ConPTY probe under synthetic load (#499):
// immediate CR submitted 1/20, fixed 350 ms 19/20, fixed 1000 ms 19/20, a
// bare quiet-window 13/20 (under load the echo itself is deferred, so it
// fires early) — this echo-then-quiet protocol was the only one to run
// clean 20/20. Short sends stay instant. Threshold: dictations that
// reproduced the swallow are ~1–2 KB; typed prompts stay well under this.
const _BULK_SUBMIT_THRESHOLD_CHARS = 500;
const _BULK_FLOOR_MS = 350;
const _BULK_QUIET_MS = 350;
const _BULK_CAP_MS = 3000;

export function growComposeInput() {
  // Auto-grow up to _COMPOSE_MAX_ROWS; the iOS return key adds newlines,
  // only ➤ Send forwards to the PTY.
  const ta = els.terminalComposeInput;
  ta.style.height = 'auto';
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height =
    Math.min(ta.scrollHeight, _COMPOSE_MAX_ROWS * lineHeight + 16) + 'px';
  // Keep the caret (end of a freshly inserted transcript) in view.
  ta.scrollTop = ta.scrollHeight;
}

export function resetComposeBar() {
  els.terminalComposeBar.hidden = true;
  els.terminalComposeInput.value = '';
  els.terminalComposeInput.style.height = '';
  // #450: an emptied buffer no longer carries an attached image path.
  if (state.terminal) state.terminal.composeHasImage = false;
}

function setComposeOpen(open) {
  const t = state.terminal;
  if (!t) return;
  t.composeOpen = open;
  els.terminalComposeBar.hidden = !open;
  if (open) {
    // Focusing the textarea pops the phone keyboard with predictive on.
    els.terminalComposeInput.focus();
  } else if (t.term) {
    // Direct mode resumes — hand focus back to xterm.
    t.term.focus();
  }
}

// Wrap a clipboard / compose payload in bracketed-paste markers (DECSET
// 2004) when the agent's TUI has them enabled, so it buffers the whole
// block as one atomic paste instead of absorbing a per-keystroke burst —
// which the Windows console input queue silently drops spans of under a
// multi-KB load (#64). This is exactly what xterm already does for its own
// native paste (term.onData); the 📋 button and compose ➤ Send bypass
// xterm, so they have to replicate it. Only bracket when the app actually
// asked for it (`term.modes.bracketedPasteMode`) — otherwise the literal
// `\x1b[200~` would land as garbage in an agent that doesn't grok it.
//
// Framing only — this never appends the submitting carriage return. A
// submit goes through `sendSubmit`, which delivers the CR as its OWN WS
// frame after this block (see #166).
export function framePaste(t, text) {
  const bracketed = !!(t.term && t.term.modes && t.term.modes.bracketedPasteMode);
  if (!bracketed) return text;
  return '\x1b[200~' + text + '\x1b[201~';
}

// Send a composed prompt to the PTY and submit it. The submitting carriage
// return is sent as its OWN WS frame *after* the (possibly bracketed) text
// block — never concatenated onto it.
//
// Why split: the webapp proxies each WS `input` frame to the session-host
// as a distinct `pty.write()`, so two frames become two PTY writes. That
// guarantees the `\x1b[201~` paste-end marker is written — and the TUI has
// finished exiting bracketed-paste mode — before the bare CR arrives. When
// the CR rode in the same frame as the end marker, the TUI intermittently
// absorbed it into paste finalization instead of running the prompt: the
// "➤ Send sometimes does nothing" race of #166. A CR *inside* the markers
// is literal pasted text by design, so the split is the only ordering that
// reliably submits. With bracketed mode off there is no paste state machine
// to race, but the two-frame path is harmless there, so it stays uniform.
// `opts.submitDelayMs` (issue #450): hold the submitting CR back by this
// many ms instead of sending it in the same burst as the text. Needed when
// the payload carries a pasted image path — Claude Code's path→attachment
// conversion absorbs a CR arriving mid-conversion, so the prompt lands
// unsubmitted and needs a second Enter. A short defer lets the conversion
// settle before the CR arrives.
// `opts.bulkSettle` (issue #499): hold the CR until the session's output
// stream shows the paste was ingested and settled (echo seen after the send,
// then quiet — floor/quiet/cap constants above). Used for dictation-sized
// text payloads, whose ingest under machine load outlives any fixed delay.
// Short plain-text sends pass no options and stay instant (the CR still
// goes as its own frame, preserving #166's ordering fix).
export function sendSubmit(t, text, opts) {
  if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
  const sentAt = Date.now();
  t.ws.send(JSON.stringify({ type: 'input', data: framePaste(t, text) }));
  const submit = function () {
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: '\r' }));
    }
  };
  if (opts && opts.bulkSettle) {
    const watch = setInterval(function () {
      const now = Date.now();
      const settled = now - sentAt >= _BULK_FLOOR_MS
        && t.lastOutputAt > sentAt
        && now - t.lastOutputAt >= _BULK_QUIET_MS;
      if (settled || now - sentAt >= _BULK_CAP_MS) {
        clearInterval(watch);
        submit();
      }
    }, 50);
    return;
  }
  const delay = opts && opts.submitDelayMs;
  if (delay > 0) setTimeout(submit, delay); else submit();
}

export function wireCompose() {
  els.terminalCompose.addEventListener('click', function () {
    const t = state.terminal;
    if (!t) return;
    setComposeOpen(!t.composeOpen);
  });
  // General attach (issue #366): second entry point into the existing
  // sendImage()/#terminalImageInput flow (terminal.js owns the change
  // handler) — reachable from the compose-bar view, unlike the outer-bar
  // 🖼 button. No accept filter, so iOS offers Files as well as Photos.
  if (els.terminalComposeAttach) {
    els.terminalComposeAttach.addEventListener('click', function () {
      els.terminalImageInput.click();
    });
  }
  els.terminalComposeSend.addEventListener('click', function () {
    const t = state.terminal;
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    const text = els.terminalComposeInput.value;
    if (!text) return;
    // #499: bulk text (a long dictation) holds the CR until the paste's
    // ingest visibly settles — under machine load a fixed defer still lands
    // mid-ingest and the CR becomes a newline instead of Submit. The settle
    // watch also covers #450's image-conversion window when both apply.
    // #450: an image path in a short buffer keeps its fixed conversion defer.
    const opts = text.length >= _BULK_SUBMIT_THRESHOLD_CHARS
      ? { bulkSettle: true }
      : (t.composeHasImage
        ? { submitDelayMs: _IMAGE_SUBMIT_DELAY_MS } : undefined);
    sendSubmit(t, text, opts);
    t.composeHasImage = false;
    els.terminalComposeInput.value = '';
    els.terminalComposeInput.style.height = '';
    els.terminalComposeInput.focus();
  });
  els.terminalComposeInput.addEventListener('input', growComposeInput);
}
