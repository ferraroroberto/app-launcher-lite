/* Terminal sizing: the keyboard-aware overlay pin, the fullscreen-agent pan,
 * resize-frame dedupe and the window/viewport listeners that drive them.
 * terminal.js wires these onto each terminal object.
 */

import { els, state } from './state.js';
import { fetchSessions } from './sessions.js';
import { beginRepaintBatch } from './terminal-connection.js';

// Given the layout-viewport height and the current visual-viewport
// height, return the pixel height to pin the terminal overlay to so its
// bottom edge sits at the top of the on-screen keyboard — or null to
// release the override and let the overlay fill the screen via the CSS
// (100dvh). iOS shrinks `visualViewport.height` when the software
// keyboard slides up but does NOT shrink the layout viewport, so a
// `position:fixed; inset:0` overlay keeps covering the whole screen
// *behind* the keyboard and the active prompt row renders hidden under
// it. Only a substantial shrink counts as the keyboard;
// smaller URL-bar / home-indicator chrome changes (<~120px) are left to
// the existing 100dvh + fit() path so this doesn't fight that behaviour.
const _KEYBOARD_SHRINK_PX = 120;

export function keyboardOverlayHeight(layoutHeight, visualHeight) {
  if (!(layoutHeight > 0) || !(visualHeight > 0)) return null;
  if (layoutHeight - visualHeight > _KEYBOARD_SHRINK_PX) {
    return Math.round(visualHeight);
  }
  return null;
}

// Pixels to shift a full-screen TUI's canvas *up* so its bottom row (the
// agent's prompt/composer) sits just above the on-screen keyboard, given
// the rendered content height and the visible box height. For a fullscreen
// differential agent (ratatui-style) the phone must NOT reflow xterm to the
// smaller keyboard box — reflowing changes the PTY rows, which SIGWINCHes
// the agent into repainting its whole frame on every keyboard open/close
// (the visible "refreshment"). Instead we keep the PTY at its
// stable size and pan the fixed canvas: translate it up by the overflow so
// the bottom stays visible while the top scrolls off behind the chrome.
// Clamped at 0 so a canvas already shorter than the box never shifts down.
export function terminalPanY(contentHeight, visibleHeight) {
  if (!(contentHeight > 0) || !(visibleHeight > 0)) return 0;
  return Math.max(0, Math.round(contentHeight - visibleHeight));
}

function pinOverlayToKeyboard() {
  // Pin the overlay to the visual viewport when the keyboard is up so
  // its bottom edge lands at the top of the keyboard and the prompt
  // stays visible — then fit() reflows xterm to the smaller box.
  // iOS doesn't just shrink the visual viewport for the
  // keyboard, it also shifts it *down* (visualViewport.offsetTop > 0)
  // to sweep the focused line into view; a position:fixed; inset:0
  // overlay is anchored to the layout-viewport top, so unless we match
  // that offset it slides up off-screen — clipping the top rows and
  // exposing a band of the page behind it just above the keyboard.
  // Track both the height and the offset. Released (back to CSS 100dvh)
  // when the keyboard hides. Must run *before* fit() so it measures the
  // new host size.
  const vp = window.visualViewport;
  const kbH = (vp && els.terminalOverlay)
    ? keyboardOverlayHeight(window.innerHeight, vp.height) : null;
  if (vp && els.terminalOverlay) {
    if (kbH != null) {
      els.terminalOverlay.style.height = kbH + 'px';
      els.terminalOverlay.style.bottom = 'auto';
      els.terminalOverlay.style.top = Math.round(vp.offsetTop || 0) + 'px';
    } else {
      els.terminalOverlay.style.height = '';
      els.terminalOverlay.style.bottom = '';
      els.terminalOverlay.style.top = '';
    }
  }
}

function panFullscreenCanvas(term) {
  const screen = term.element &&
    term.element.querySelector('.xterm-screen');
  const contentH = screen ? screen.getBoundingClientRect().height : 0;
  // Pan against the HOST's real box, not the visual-viewport height:
  // the overlay also holds the header bar and the
  // compose bar, so the viewport height over-states the terminal's
  // visible box by their combined height — under-panning by exactly
  // that much and hiding the bottom rows (the prompt echo) behind
  // the compose bar whenever the native keyboard is up. The overlay
  // pinning above already resized the flex layout, so the host rect
  // is the box the canvas actually shows through.
  const boxH = els.terminalHost ?
    els.terminalHost.getBoundingClientRect().height : 0;
  const panY = terminalPanY(contentH, boxH);
  if (term.element) {
    term.element.style.transform =
      panY ? ('translateY(-' + panY + 'px)') : '';
  }
}

function sendResizeIfChanged(t, term) {
  if (t.ws && t.ws.readyState === WebSocket.OPEN) {
    // Same-size dedupe: a resize frame that changes nothing
    // still costs a setwinsize round-trip; skip it. A real change on a
    // fullscreen agent triggers the transcript re-emission — batch it
    // into a single paint (terminal-connection.js).
    const size = term.rows + 'x' + term.cols;
    if (size !== t.lastSentSize) {
      t.lastSentSize = size;
      if (t.isFullscreen) beginRepaintBatch(t);
      t.ws.send(JSON.stringify({
        type: 'resize', rows: term.rows, cols: term.cols,
      }));
    }
  }
}

export function applyTerminalSize(t, session) {
  const term = t.term;
  const fit = t.fit;
  const sid = t.sid;
  const isMirror = t.mirror;
  // A stashed warm terminal is inert: its resize listeners stay
  // bound while hidden, but only the ACTIVE terminal may touch the
  // shared overlay chrome or its own layout. Resume re-runs applySize.
  if (t !== state.terminal) return;
  if (isMirror) {
    // Match the phone's PTY dimensions; never touch the PTY itself.
    const s = (state.sessions || []).find(function (x) {
      return x.session_id === sid;
    });
    const cols = (s && s.cols) || session.cols || 120;
    const rows = (s && s.rows) || session.rows || 40;
    try { term.resize(cols, rows); } catch (_) {}
    return;
  }
  pinOverlayToKeyboard();
  // Full-screen differential agent: once sized, the PTY is PINNED for the
  // rest of the session's lifetime — no further reflow, ever.
  // EVERY change after the first real size (keyboard up/down, compose
  // bar, browser chrome, a PWA layout-viewport shrink, OR a phone
  // rotation) PANS/letterboxes the fixed-size canvas instead.
  // Empirical (probed): a ratatui-style TUI re-emits its
  // ENTIRE transcript on any winsize change — rows or cols, either
  // direction (~65 KB on a long conversation) — so a single stray
  // SIGWINCH replays the whole conversation through the phone terminal.
  // This app declares no manifest `orientation` member and never calls
  // `ScreenOrientation.lock()`, so rotation isn't blocked at the platform
  // level — pinning cols makes it harmless instead: landscape
  // just shows the same portrait-width canvas letterboxed (xterm sizes
  // its own canvas off `cols`, not the host box, so no extra CSS is
  // needed). The host is overflow:hidden, so panned-off top rows clip
  // cleanly; panning by the content-vs-box overflow also covers the
  // keyboard-down case (overflow 0 → transform cleared). Claude (inline)
  // and the fullscreen first-fit case fall through to the reflow path
  // below, which also clears the pan.
  // (The pan shortcut additionally requires a size to have been SENT —
  // the pre-WS first fit sets fsSized, but the phone's authoritative
  // size must still go out on the first open.)
  if (t.isFullscreen && t.fsSized && t.lastSentSize) {
    panFullscreenCanvas(term);
    return;
  }
  if (term.element) term.element.style.transform = '';
  try { if (fit) fit.fit(); } catch (_) {}
  // Keep the prompt (bottom row) in view after a keyboard-driven
  // reflow, but only if the user hadn't scrolled up to read history.
  try {
    const b = term.buffer.active;
    if (b.viewportY >= b.baseY - 1) term.scrollToBottom();
  } catch (_) {}
  sendResizeIfChanged(t, term);
  t.fsSized = true;
}

export function bindTerminalResizeHandling(t, applySize) {
  if (t.mirror) {
    // The phone may rotate or resize — re-sync to its size periodically.
    t.sizeTimer = setInterval(function () {
      fetchSessions().then(applySize).catch(function () {});
    }, 2500);
  } else {
    setTimeout(applySize, 0);
    t.onWindowResize = applySize;
    window.addEventListener('resize', applySize);
    // iOS doesn't fire 'resize' when its chrome (URL bar / home
    // indicator) shows or hides — those changes ride on the
    // visualViewport API instead. Without re-fitting, xterm keeps
    // its old row count and the freed pixels show as a dead black
    // band at the bottom of the overlay.
    if (window.visualViewport) {
      t.onVisualViewport = applySize;
      window.visualViewport.addEventListener('resize', applySize);
      // Keyboard-driven shifts of visualViewport.offsetTop (iOS sweeping
      // the focused line into view) ride on 'scroll', not 'resize' — wire
      // it too so the overlay re-tracks the offset mid-sweep instead of
      // leaving a band of the page behind it above the keyboard.
      window.visualViewport.addEventListener('scroll', applySize);
    }
    // A portrait/landscape rotation cycle can leave
    // window.innerHeight and visualViewport.height transiently mismatched
    // while iOS's chrome bars settle, so a 'resize'/'visualViewport resize'
    // event mid-rotation can sample keyboardOverlayHeight() at a bad
    // moment and pin the overlay to a stale shrunk height — with no
    // guaranteed later event to release it, leaving the session list/
    // Projects grid bleeding through underneath. 'orientationchange' fires
    // once per rotation regardless of that race: release any pin
    // immediately so the overlay is never stuck small, then re-run
    // applySize() once the viewport has settled so a keyboard that's
    // genuinely still open gets correctly re-pinned.
    t.onOrientationChange = function () {
      // Stashed (warm-cached) terminals keep this listener bound
      // while hidden — same as applySize()'s own guard, only the ACTIVE
      // terminal may touch the shared overlay chrome.
      if (t !== state.terminal) return;
      if (els.terminalOverlay) {
        els.terminalOverlay.style.height = '';
        els.terminalOverlay.style.bottom = '';
        els.terminalOverlay.style.top = '';
      }
      if (t.orientationSettleTimer) clearTimeout(t.orientationSettleTimer);
      t.orientationSettleTimer = setTimeout(applySize, 350);
    };
    window.addEventListener('orientationchange', t.onOrientationChange);
  }
}
