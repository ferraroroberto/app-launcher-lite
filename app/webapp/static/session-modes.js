/* Which views a session can offer, and the one it opens in.
 *
 * Pure reads of a session object and localStorage, kept apart from the
 * views themselves: the session list and the Board draw their Chat, Links
 * and mode controls from these at first load, while the chat, terminal and
 * links code loads only when one of them first opens (lazy-views.js).
 */

import { state } from './state.js';
import { opensInChatFirst } from './api.js';

// Agents whose native history the server-side reader understands. Decides
// Chat availability without a probe; the pane still shows the server's own
// reason line if it disagrees. An absent agent field (a ?session= deep
// link's bare {session_id, name}) defaults to Copilot.
export const TRANSCRIPT_AGENTS = ['copilot'];

// Whether Chat can read `s`'s history.
export function hasTranscriptReader(s) {
  const agent = String((s && s.agent) || 'copilot').toLowerCase();
  return TRANSCRIPT_AGENTS.indexOf(agent) !== -1;
}

// Links reads the same transcript, so both ⋮ menus gate the item on it.
export function hasLinks(s) {
  return !!s && hasTranscriptReader(s);
}

export function terminalAvailable(s) {
  return !!s && s.kind !== 'remote';
}

export function chatAvailable(s) {
  return hasTranscriptReader(s);
}

function agentLabel(s) {
  const known = (state.agents || []).find(function (a) { return a.id === s.agent; });
  return known ? known.label : (s.agent || 'this agent');
}

// The one-line reason a segment is off, shown as the segment's title and
// toasted on tap (a phone has no hover).
export function modeReason(s, mode) {
  if (mode === 'terminal') return 'Detached session — no terminal';
  return 'No transcript reader for ' + agentLabel(s);
}

// Last-viewed mode per session id — one JSON map under one key rather than
// a key per session, capped so ended sessions don't accumulate forever.
const MODES_KEY = 'launcher.sessionModes';
const MODES_MAX = 50;

function readModes() {
  try {
    const raw = localStorage.getItem(MODES_KEY);
    const obj = raw ? JSON.parse(raw) : {};
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {};
  } catch (_) {
    return {};
  }
}

function lastMode(sid) {
  const m = readModes()[sid];
  return m === 'chat' || m === 'terminal' ? m : null;
}

export function rememberMode(sid, mode) {
  try {
    const modes = readModes();
    // Re-insert so insertion order doubles as LRU.
    delete modes[sid];
    modes[sid] = mode;
    const keys = Object.keys(modes);
    while (keys.length > MODES_MAX) delete modes[keys.shift()];
    localStorage.setItem(MODES_KEY, JSON.stringify(modes));
  } catch (_) { /* private mode / quota: the mode just isn't remembered */ }
}

// The mode a session opens in: an explicit choice, else the one it was last
// viewed in (per session, remembered), else Chat — or Terminal with the
// "Open in Chat" setting off — clamped to the panes this session has. The
// row-tap's PC-window gate (sessions.js) resolves through this too, so
// only a session that resolves to Terminal can leave for the PC window.
export function resolveSessionMode(session, wantMode) {
  const termOK = terminalAvailable(session);
  const chatOK = chatAvailable(session);
  let mode = wantMode || lastMode(session.session_id) ||
    (chatOK && opensInChatFirst() ? 'chat' : (termOK ? 'terminal' : 'chat'));
  if (mode === 'chat' && !chatOK) mode = 'terminal';
  if (mode === 'terminal' && !termOK) mode = 'chat';
  return mode;
}
