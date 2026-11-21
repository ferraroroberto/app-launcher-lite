/* Session overlay mode routing.
 *
 * One overlay (#terminalOverlay), two render modes of the same session:
 *
 *   terminal — the live PTY (terminal.js): xterm + WebSocket + composer.
 *   chat     — the agent's own history as chat-style cards
 *              (session-transcript.js), for any session kind whose agent
 *              has a transcript reader.
 *
 * Switching modes shows/hides a pane; nothing reconnects or reloads. A
 * connected terminal stays connected while Chat is showing (its resize
 * path is inert, see terminal.js applySize), and the chat pane keeps its
 * pages and fold state while Terminal is showing. The terminal is
 * connected lazily — opening straight into Chat never runs the passkey /
 * WebSocket path until Terminal is first tapped.
 *
 * Availability: Terminal needs a PTY (`kind !== 'remote'`); Chat needs a
 * reader for the agent. A segment the session can't offer is aria-disabled
 * and a tap on it toasts the reason. A detached session opens in Chat; a
 * PTY session of an agent with no reader opens in Terminal; otherwise the
 * mode the session was last viewed in wins (remembered per session id in
 * localStorage), defaulting to Chat (Terminal with the Settings switch off). A session that can offer *neither*
 * (detached, no reader) still opens, in Chat, on the reader's reason line
 * — the overlay is the only place its Rename and Stop live.
 */

import { els, state } from './state.js';
import { toast } from './api.js';
import { chatAvailable, modeReason, rememberMode, resolveSessionMode, terminalAvailable } from './session-modes.js';
import {
  attachTerminalPane,
  hideTerminal,
  showSessionShell,
  stashActiveTerminal,
} from './terminal.js';
import {
  chatPaneSession,
  closeChatComposerPopovers,
  closeChatPane,
  openChatPane,
  syncLiveRefresh,
} from './chat/session-transcript.js';
import { pinChatToKeyboard } from './chat/chat-send.js';
import { applyTermTheme } from './terminal/terminal-theme.js';
import { loadXterm } from './terminal/xterm-loader.js';
import { closeTerminalMenu, updateLatestPill } from './terminal/terminal-bar.js';
import { syncContextRing, wireContextRing } from './context-ring.js';
import { wireModelChip } from './terminal/model-chip.js';
import { terminalComposer } from './terminal/terminal-compose.js';
import { isWideLayout } from './layout.js';
import { setTab } from './tabs.js';
import { bindLongPressHint } from './long-press-hint.js';

// After a session opens, how long the xterm prefetch waits: past the Chat
// pane's first paint on a slow phone (~0.5 s measured at 4x CPU throttle),
// well before a reader taps Terminal.
const XTERM_PREFETCH_DELAY_MS = 700;

function inChatMode() {
  return !!els.terminalOverlay && els.terminalOverlay.dataset.mode === 'chat';
}

function segment(mode) {
  return mode === 'chat' ? els.sessionModeChat : els.sessionModeTerminal;
}

function syncSegment(mode, session) {
  const btn = segment(mode);
  if (!btn) return;
  const ok = mode === 'chat' ? chatAvailable(session) : terminalAvailable(session);
  const label = mode === 'chat' ? 'Chat' : 'Terminal';
  if (ok) {
    btn.removeAttribute('aria-disabled');
    // What the long-press hint and a desktop hover say.
    btn.title = mode === 'chat' ? 'Show the chat' : 'Show the terminal';
    btn.setAttribute('aria-label', label);
  } else {
    btn.setAttribute('aria-disabled', 'true');
    btn.title = modeReason(session, mode);
    btn.setAttribute('aria-label', label + ' — ' + btn.title);
  }
}

function syncPressed(mode) {
  ['terminal', 'chat'].forEach(function (m) {
    const btn = segment(m);
    if (btn) btn.setAttribute('aria-pressed', m === mode ? 'true' : 'false');
  });
}

// Release a keyboard pin left by whichever pane was showing, so the
// next pane starts from the CSS-driven full height; the pane re-pins itself
// if the keyboard is genuinely still up.
function releasePin() {
  const o = els.terminalOverlay;
  if (!o) return;
  o.style.height = '';
  o.style.bottom = '';
  o.style.top = '';
}

export function setSessionMode(mode) {
  const v = state.sessionView;
  if (!v) return;
  const s = v.session;
  if (mode === 'chat' && !chatAvailable(s)) mode = 'terminal';
  if (mode === 'terminal' && !terminalAvailable(s)) mode = 'chat';
  const changed = v.mode !== mode;
  v.mode = mode;
  els.terminalOverlay.dataset.mode = mode;
  syncPressed(mode);
  rememberMode(s.session_id, mode);
  if (changed) {
    releasePin();
    closeTerminalMenu();
    terminalComposer.closePopovers();
    closeChatComposerPopovers();
  }
  // The chat pane's live refresh follows the mode: showing Terminal
  // stops it, showing Chat starts it. Called for both so a window sitting on
  // Terminal never fetches chat for the session it is showing — the "10
  // windows, only one being read" constraint the feature exists for.
  syncLiveRefresh();
  // The context ring polls its own route in both modes; a switch
  // reads it once more straight away.
  syncContextRing();
  if (mode === 'terminal') {
    const t = state.terminal;
    if (t && t.sid === s.session_id) {
      // Already connected (or warm): just re-measure the pane it hid behind.
      if (t.applySize) t.applySize();
      updateLatestPill();
      try { t.term.focus(); } catch (_) { /* no xterm yet */ }
    } else {
      attachTerminalPane(s);
    }
  } else {
    if (chatPaneSession() !== s.session_id) openChatPane(s);
    updateLatestPill();
    pinChatToKeyboard();
  }
  // The user background override paints the overlay only behind the
  // terminal; the chat pane is a reading surface on the app canvas.
  applyTermTheme();
}

// The wide layout's list-and-detail: the row whose session is in the
// docked view keeps the accent tint. The mark is harmless below 1100px,
// where the view covers the list; CSS only paints it at the wide query.
export function markSelectedSession(sid) {
  document.querySelectorAll('#sessionsList li.session-item').forEach(function (li) {
    if (sid && li.dataset.sessionId === sid) li.setAttribute('aria-current', 'true');
    else li.removeAttribute('aria-current');
  });
}

export function openSessionOverlay(session, wantMode) {
  if (!session || !session.session_id) return;
  // On a wide window the view is the Code tab's detail pane, so a session
  // opened from elsewhere (the Board drawer) lands on the Code tab.
  if (isWideLayout() && state.tab !== 'claude') setTab('claude');
  // A session that can offer neither pane still opens: both segments
  // render aria-disabled and the chat pane shows the reader's own reason
  // line, which beats an unexplained dead row. That shape — detached, agent
  // with no reader — is the only way Rename and Stop stay reachable for it
  // now that the row gear is gone, and they live in the bar's ⋮ menu.
  const mode = resolveSessionMode(session, wantMode);
  // The xterm stack costs about a second to fetch and parse on a phone, and
  // loading it waits for the first terminal. Start it soon after a session
  // that has one opens, so a tap on Terminal finds it already there; the
  // delay keeps its parse off the main thread while Chat paints its first
  // page. A Terminal open loads it straight away itself, and reports a
  // failure, so one here is dropped.
  if (terminalAvailable(session)) {
    window.setTimeout(function () {
      const v = state.sessionView;
      if (v && v.session.session_id === session.session_id) loadXterm().catch(function () {});
    }, XTERM_PREFETCH_DELAY_MS);
  }
  // Panes left showing another session are stale for this one: the chat
  // pane is closed, and an active terminal is stashed warm so its
  // title poll can't keep writing the old title over this session's bar
  // while Chat is showing (Terminal mode re-attaches through the cache).
  if (chatPaneSession() && chatPaneSession() !== session.session_id) closeChatPane();
  if (state.terminal && state.terminal.sid !== session.session_id) stashActiveTerminal();
  showSessionShell(session);
  state.sessionView = { session: session, mode: null };
  syncSegment('terminal', session);
  syncSegment('chat', session);
  setSessionMode(mode);
  markSelectedSession(session.session_id);
}

export function closeSessionOverlay() {
  state.sessionView = null;
  syncContextRing();
  closeChatPane();
  if (els.terminalOverlay) delete els.terminalOverlay.dataset.mode;
  hideTerminal();
  markSelectedSession(null);
}

export function wireSessionModeToggle() {
  // A wide window shows the rail beside the docked view, so another tab is
  // one click away. The view belongs to the Code tab's list: leaving it
  // closes the view (a terminal stays warm), rather than leaving a
  // hidden chat polling for a reader who has gone.
  document.addEventListener('launcher:tab', function (ev) {
    const tab = ev.detail && ev.detail.tab;
    if (tab !== 'claude' && state.sessionView && isWideLayout()) closeSessionOverlay();
  });
  wireContextRing();
  wireModelChip();
  if (!els.sessionMode) return;
  bindLongPressHint(els.sessionModeTerminal);
  bindLongPressHint(els.sessionModeChat);
  els.sessionMode.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.session-mode-btn');
    if (!btn || !state.sessionView) return;
    const mode = btn.dataset.mode;
    if (btn.getAttribute('aria-disabled') === 'true') {
      toast(modeReason(state.sessionView.session, mode), '', {
        icon: mode === 'terminal' ? 'cloud' : 'messages-square',
      });
      return;
    }
    setSessionMode(mode);
  });
}

export { inChatMode };
