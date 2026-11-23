/* HTTP helpers + bearer-token plumbing + sign-in + toast + clipboard copy.
 *
 * Every module reaches the server through api()/jsonApi() so the
 * Authorization: Bearer header is attached once, and 401s open the
 * vendored sign-in sheet from one place.
 */

import { els, state, TOKEN_KEY } from './state.js';
import { showToast as toast } from './_vendored/toast/toast.js';
import { escapeHtml } from './dom-utils.js';

// --------------------------------------------------------------- tokens
// Read a `?<name>=<value>` deep-link param once, strip it from the visible
// URL, and return its trimmed value (or null if absent/blank). Used for the
// bearer ?token=, PC-mirror ?terminal=<sid>, shared ?session=<sid>, Board
// ?board=<sid> deep link.
export function consumeUrlParam(name) {
  const params = new URLSearchParams(window.location.search);
  const v = (params.get(name) || '').trim();
  if (!v) return null;
  params.delete(name);
  const q = params.toString();
  window.history.replaceState(
    {}, '',
    window.location.pathname + (q ? '?' + q : '') + window.location.hash
  );
  return v;
}

export function readToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
export function writeToken(t) { if (t) localStorage.setItem(TOKEN_KEY, t); }

// Build request headers: bearer token (always, when present) plus the
// passkey terminal token, when the caller passes one — callers own how they
// obtained it (readTerminalToken() for the cached value, or an awaited
// ensureTerminalToken() when a fresh passkey ceremony may be needed), since
// that plumbing lives in webauthn.js and this module never imports it.
// `contentType`, when given, sets Content-Type (omit for multipart/FormData
// bodies, where the browser must set its own boundary). The single source of
// the terminal-token header, always spelled `X-Terminal-Token`.
export function authHeaders({ terminalToken, contentType } = {}) {
  const h = {};
  const bearer = readToken();
  if (bearer) h['Authorization'] = 'Bearer ' + bearer;
  if (terminalToken) h['X-Terminal-Token'] = terminalToken;
  if (contentType) h['Content-Type'] = contentType;
  return h;
}

// True when this browser is a desktop (a fine/mouse pointer), as opposed
// to a phone (a coarse/touch pointer). A coding launch carries this so the
// server opens a dedicated PC mirror window rather than rendering the
// terminal inside the desktop browser's own tab (the "obvious"
// The optimization of skipping a redundant in-page render was reversed:
// that redundancy was what let Stop & Close tear down the controlling
// browser window). The flag is what distinguishes a desktop from a phone
// regardless of loopback vs tunnel, since the server can't tell by IP alone.
export function isDesktopClient() {
  try {
    return window.matchMedia('(pointer: fine)').matches;
  } catch (exc) {
    return false;
  }
}

// Settings > Sessions: a desktop browser opens a session's terminal in the
// dedicated PC Edge window only when the owner turned that on (off by
// default). The phone never does. Before the config lands this reads off,
// so a boot-time tap renders in the page rather than spawning a window.
export function opensInPcWindow() {
  return isDesktopClient() &&
    !!(state.config && state.config.open_sessions_in_window === true);
}

// Settings > Sessions: a session with no remembered view opens in Chat
// (default on). Reads on before the config lands, matching the default.
export function opensInChatFirst() {
  return !state.config || state.config.open_sessions_in_chat !== false;
}

// A 401 means the sign-in sheet just went up — callers that merely want to
// swallow "not logged in yet" (rather than toast it as a real failure) check
// `exc instanceof AuthRequiredError`, not a string message that a future
// reword would silently break.
// The one place the `/api/claude-code/sessions/<sid>` prefix is built. `tail`
// is appended verbatim and starts with `/` or `?`.
export function sessionRoute(sid, tail) {
  return '/api/claude-code/sessions/' + encodeURIComponent(sid) + (tail || '');
}

export class AuthRequiredError extends Error {
  constructor() {
    super('auth required');
    this.name = 'AuthRequiredError';
  }
}

// Log a background-poll failure, unless it's just the sign-in sheet going
// up — every poll loop (apps, sessions, jobs, team-os, ...)
// wants the exact same "warn on real failures, stay silent on 401" guard.
export function logPollFailure(label, exc) {
  if (exc instanceof AuthRequiredError) return;
  console.warn(label, exc);
}

// --------------------------------------------------------------- fetch
// index.html's inline boot script starts the first panel reads while the
// module graph is still loading (window.__bootPrefetch, path -> { at, res }).
// A plain GET of one of those paths takes the response once; any later call,
// or one past this age (a boot that stalled on sign-in), fetches afresh.
const BOOT_PREFETCH_MAX_AGE_MS = 15000;

function takeBootPrefetch(path, opts) {
  const store = window.__bootPrefetch;
  if (!store || opts.method || opts.body || opts.headers || opts.signal) return null;
  const hit = store[path];
  if (!hit) return null;
  delete store[path];
  if (performance.now() - hit.at > BOOT_PREFETCH_MAX_AGE_MS) return null;
  return hit.res.catch(function () { return null; });
}

export async function api(path, opts) {
  opts = opts || {};
  const early = takeBootPrefetch(path, opts);
  const prefetched = early ? await early : null;
  const headers = new Headers(opts.headers || {});
  const token = readToken();
  if (token) headers.set('Authorization', 'Bearer ' + token);
  const res = prefetched || await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    showLogin();
    throw new AuthRequiredError();
  }
  return res;
}

// The parsed JSON body of `res` (null when there is none), or a thrown
// `Error(detail || 'HTTP n')` carrying `.status` / `.body` on a non-2xx. The
// one place a response becomes a verdict: jsonApi() and apiRawJson() both end
// here.
async function jsonVerdict(res) {
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) {
    const detail = (body && body.detail) || ('HTTP ' + res.status);
    const err = new Error(detail);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

export async function jsonApi(path, opts) {
  return jsonVerdict(await api(path, opts));
}

// A raw-Response counterpart to jsonApi() for callers that need the Response
// itself (FormData uploads, streamed audio bodies) instead of jsonApi()'s
// parsed-JSON contract. Folds authHeaders() in so a caller
// doesn't have to build the passkey terminal-token / content-type headers by
// hand — pass them as `opts.terminalToken` / `opts.contentType` alongside the
// usual fetch options — then goes through the exact same 401 → showLogin() +
// AuthRequiredError path as jsonApi(), via api(). Callers still own res.ok /
// res.json() beyond that, same as they did calling fetch() directly before.
// `opts.signal` rides through to fetch() untouched: the composer's upload and
// dictation requests pass one so a second tap can abort them.
export async function apiRaw(path, opts) {
  opts = opts || {};
  const headers = Object.assign(
    {},
    authHeaders({ terminalToken: opts.terminalToken, contentType: opts.contentType }),
    opts.headers || {}
  );
  return api(path, Object.assign({}, opts, { headers: headers }));
}

// apiRaw() + jsonApi()'s verdict: resolves the parsed JSON body (null
// when there is none) and throws `Error(detail || 'HTTP n')` with `.status` /
// `.body` on a non-2xx, so an upload / transcribe caller states neither the
// `res.ok` check nor the error-body parse. A caller that must read the status
// itself without throwing (a retried chunk upload) keeps apiRaw().
export async function apiRawJson(path, opts) {
  return jsonVerdict(await apiRaw(path, opts));
}

// --------------------------------------------------------------- sign-in
// Called from every 401. The sheet hands every caller the same promise while
// it is open, so the follow-up runs once per sign-in, not once per 401. Its
// code loads on the first 401: a signed-in boot never draws it.
let onSignedIn = null;
let signInOpen = false;

export function showLogin() {
  if (signInOpen) return;
  signInOpen = true;
  import('./_vendored/sign-in/sign-in.js').then(function (m) {
    return m.requestSignIn({
      endpoint: '/api/login',
      hint: 'Enter the password to use the launcher.',
      onToken: writeToken,
    });
  }).then(function () {
    signInOpen = false;
    if (onSignedIn) onSignedIn();
  }, function (exc) {
    signInOpen = false;
    console.warn('sign-in failed to open', exc);
  });
}

// Boot hook called from main.js — passed `onLoginSuccess` so this module
// stays independent of the boot sequence.
export function setSignInHandler(onLoginSuccess) {
  onSignedIn = onLoginSuccess;
}

// --------------------------------------------------------------- toast
// escapeHtml lives in dependency-free dom-utils.js so the markdown renderer
// can import it without this module's DOM/localStorage-bound state;
// re-exported here so existing `from './api.js'` importers are unchanged.
export { escapeHtml };

// The fleet's vendored toast: toast(msg, kind, { icon, host, ms, action }).
// Only 'error' tints; every other kind renders neutral. opts.host is a
// .sheet-toast inside an open modal <dialog>: the page's own toast sits under
// a dialog's top layer, so a sheet that must confirm an action carries one.
export { toast };

// Toast a failed API call as "<prefix>: <message>" (error styling) — unless
// exc is an AuthRequiredError, in which case the sign-in sheet already went
// up and a red toast on top of it would double-fire. `prefix` may be falsy
// for the few sites that just toast the raw message.
//
// A plain Error carrying `.status === 401` gets the same treatment: terminal-speech.js
// (re-exported through terminal-readback.js)
// is kept free of api.js imports by design (it receives credentials as
// parameters rather than reaching into this module's token storage itself),
// so its own fetches can't throw AuthRequiredError —
// they tag `.status` on their thrown Error instead, and this is the one place
// that turns that into the same showLogin() the rest of the app gets for
// free through api()/jsonApi()/apiRaw().
export function apiFailToast(prefix, exc, opts) {
  if (exc instanceof AuthRequiredError) return;
  if (exc && exc.status === 401) { showLogin(); return; }
  const msg = (exc && exc.message) || exc;
  toast(prefix ? prefix + ': ' + msg : String(msg), 'error', opts);
}

// --------------------------------------------------------------- clipboard
// The one copy every menu uses (the Apps tab's, the PC load sheet's): `what`
// names the thing copied ("Link", "Path") in the toast; `opts.host` is the
// toast host of a sheet that carries its own (a modal dialog hides the page's
// toast).
export async function copyText(text, what, opts) {
  try {
    await writeClipboard(text, opts);
    toast(what + ' copied', 'good', opts);
  } catch (exc) {
    logPollFailure('clipboard write failed', exc);
    toast('Could not copy the ' + what.replace(/^(?=[A-Z][a-z])./, (c) => c.toLowerCase())
      + '. Copy it by hand instead', 'error', opts);
  }
}

// `writeText` needs a secure context and a focused document, and rejects
// (or is missing) where the page is served over plain http. The fallback is a
// selected hidden textarea copied with `execCommand`, still inside the tap's
// user activation. It lives inside the toast host's modal <dialog> when there
// is one: the rest of the page is inert while a modal is open, so a textarea
// outside it can never take focus.
async function writeClipboard(text, opts) {
  let failure = null;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (exc) {
      failure = exc;
    }
  }
  if (!copyWithSelection(text, opts)) {
    throw failure || new Error('the browser blocked the clipboard');
  }
}

function copyWithSelection(text, opts) {
  const root = (opts && opts.host && opts.host.closest('dialog')) || document.body;
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.setAttribute('aria-hidden', 'true');
  area.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
  const previous = document.activeElement;
  root.appendChild(area);
  try {
    area.focus();
    area.select();
    area.setSelectionRange(0, text.length);
    return document.execCommand('copy');
  } catch (exc) {
    return false;
  } finally {
    area.remove();
    if (previous && previous.focus) previous.focus();
  }
}
