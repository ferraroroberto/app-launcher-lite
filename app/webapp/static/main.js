/* Entry point: wires every module together, runs boot(), drives polls.
 *
 * Modules export named functions; this file is the only place that
 * sequences them. Each wireX() attaches DOM listeners exactly once;
 * each fetchX() refreshes its slice of state and re-renders.
 */

import { els, state, BOARD_POLL_MS, GIT_STATUS_POLL_MS, JOBS_POLL_MS, LISTENERS_POLL_MS, RUNNING_APPS_POLL_MS, SESSIONS_POLL_MS, SYSTEM_LOAD_POLL_MS, TUNNEL_POLL_MS, WEBAUTHN_POLL_MS } from './state.js';
import { AuthRequiredError, apiFailToast, consumeUrlParam, jsonApi, setSignInHandler, toast, writeToken } from './api.js';
import { wireTabs } from './tabs.js';
import { wireUsageLog } from './usage.js';
import { fetchConfig, wireClaudeOptions } from './settings/claude-options.js';
import { fetchSessions, wireSessions } from './sessions.js';
import { fetchSystemLoad, wireSystemLoad } from './system-load.js';
import { fetchAgents } from './apps/apps-reads.js';
import { adoptApps, adoptBoard, fetchApps, fetchBoard, fetchJobs, fetchListeners, fetchRunningApps, fetchSkills, markMirrorWindowEarly, openBoardCard, openScreenWindow, openTerminal, prefetchViews, refreshGitStatus, renderBoard, startSessionViews, wireViewOpeners } from './lazy-views.js';
import { fetchWebauthnStatus, wireWebauthn, writeTerminalToken } from './webauthn.js';
import { renderSettingsHead } from './home-head.js';
import { isWideLayout } from './layout.js';
import { openDesktopColumns } from './board/board-columns.js';
import { icon } from './_vendored/icons/icons.js';
import { startPoll } from './shared/poll.js';
import { pauseWhenHidden, pollMs } from './shared/perf-config.js';

// --------------------------------------------------------- settings panel
// No Edit mode: Add, Edit and Remove are always one tap away, so there is
// no edit-mode switch or persisted key. A device that still holds the
// stale key drops it here, once per boot.
try { localStorage.removeItem('launcher.editMode'); } catch (_) { /* storage blocked */ }

// Boot-autostart is its own dedicated endpoint, not a
// patchConfig() field — enabling/disabling it writes/removes a Startup-folder
// wrapper bat (a real filesystem side effect), so the click re-fetches
// /api/config for the actual on-disk state rather than optimistically
// flipping aria-checked.
async function toggleBootAutostart() {
  const next = els.bootAutostartToggle.getAttribute('aria-checked') !== 'true';
  try {
    await jsonApi('/api/settings/boot-autostart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next }),
    });
    await fetchConfig();
    toast(
      next ? 'App Launcher Lite will start at log on.' : 'Boot autostart disabled.',
      'good'
    );
  } catch (exc) {
    apiFailToast('Boot autostart failed', exc);
  }
}

function wireSettings() {
  if (els.bootAutostartToggle) {
    els.bootAutostartToggle.addEventListener('click', toggleBootAutostart);
  }
  // Every other Settings row loads with the pane's code (lazy-views.js).
}

// --------------------------------------------------------- theme toggle
function wireTheme() {
  // The pre-paint boot script in index.html already stamped
  // html[data-theme] (localStorage override, prefers-color-scheme
  // fallback); the button just flips it. The sun/moon glyph swap is pure
  // CSS keyed on the attribute, so there is nothing to re-render here —
  // the terminal screen follows the app theme via
  // terminal.js's own data-theme observer, which restyles any open
  // terminal. Every page header carries one (the home-head toggle
  // slot); they all flip the same attribute.
  document.querySelectorAll('.theme-toggle-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const dark = document.documentElement.dataset.theme !== 'dark';
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      localStorage.setItem('app-launcher.theme', dark ? 'dark' : 'light');
    });
  });
}

// --------------------------------------------------------- status readout
// Renders a sprite-icon span + a trailing text node — data (tunnel_url,
// etc.) always rides a text node, never innerHTML, even though it's
// locally-sourced.
async function fetchStatus() {
  try {
    const body = await jsonApi('/api/status');
    state.status = body;
    els.configError.textContent = body.config_error || '';
    els.configError.hidden = !body.config_error;
    // No TLS badge or tunnel URL render here: exposing the tunnel hostname
    // in the UI is needless, and not information the user needs day to
    // day. The reachability warning stays; it's actionable (fix by
    // switching to the Tailscale URL), not just informational.
    els.statusReadout.innerHTML = '';
    if (body.terminal && body.terminal.reachable === false) {
      const ic = document.createElement('span');
      ic.className = 'inline-icon';
      ic.innerHTML = icon('triangle-alert');
      els.statusReadout.appendChild(ic);
      els.statusReadout.appendChild(document.createTextNode(' terminal needs the Tailscale URL'));
    }
  } catch (_) {
    els.statusReadout.textContent = '';
  }
}

// --------------------------------------------------------- build identity
async function fetchVersion() {
  // Visible proof of which build the PWA is running. Catches stale-cache
  // confusion before it costs a debugging session. Uses jsonApi so the
  // bearer token is attached — /api/version is auth-gated like the rest.
  // A transient failure is retried on boot's backoff: one dropped
  // request used to leave the readout blank until a reload. No toast — it is
  // a readout; after the last attempt it stays blank.
  for (let attempt = 0; ; attempt++) {
    try {
      const [body, foot] = await Promise.all([
        jsonApi('/api/version'), import('./_vendored/page-foot/page-foot.js'),
      ]);
      const sha = body.git_sha || 'unknown';
      els.buildReadout.textContent = foot.buildReadoutText(sha, body.built_at || '');
      // The Settings header carries the sha too, without the time.
      state.buildSha = sha;
      renderSettingsHead();
      return;
    } catch (exc) {
      els.buildReadout.textContent = '';
      if (exc instanceof AuthRequiredError || attempt >= BOOT_RETRY_MS.length) return;
      console.warn('boot: /api/version failed, retrying', exc);
      await new Promise(function (resolve) {
        setTimeout(resolve, BOOT_RETRY_MS[attempt]);
      });
    }
  }
}

// --------------------------------------------------------- boot
// A transient /api/config failure (a dropped connection on the phone, or a
// loopback socket error on a loaded box) used to end boot() for good:
// no session list, and none of the polls armed at the end of boot(), so the
// app stayed empty until a manual reload. Non-auth failures are
// retried on this backoff before the toast. A 401 is not retried: it has
// already raised the sign-in sheet, whose success handler calls boot() again.
// fetchVersion() above retries on the same backoff.
const BOOT_RETRY_MS = [500, 1000, 2000, 4000];

async function fetchConfigForBoot() {
  for (let attempt = 0; ; attempt++) {
    try {
      await fetchConfig();
      return true;
    } catch (exc) {
      if (exc instanceof AuthRequiredError) return false;
      if (attempt >= BOOT_RETRY_MS.length) {
        apiFailToast('Boot failed', exc);
        return false;
      }
      console.warn('boot: /api/config failed, retrying', exc);
      await new Promise(function (resolve) {
        setTimeout(resolve, BOOT_RETRY_MS[attempt]);
      });
    }
  }
}

// The Screen view's pop-out window opens on ?screen=1. Read once, at
// the first boot: a login in between re-runs boot() after the param is gone.
let screenWindow = null;

async function boot() {
  if (screenWindow === null) screenWindow = consumeUrlParam('screen') !== null;
  const { mirrorSid, sharedSessionSid } = consumeSessionParams();

  if (!(await fetchConfigForBoot())) return;
  // Each remaining boot fetch fills one panel — none is load-bearing for
  // the rest of the app, so a single failure must not abort boot() and take
  // the deep-link branch below down with it: the PC mirror window's title
  // marker + terminal connect depend on reaching it.
  const safe = function (fn) { return fn().catch(function (exc) {
    console.warn('boot: non-critical fetch failed', exc);
  }); };

  // The pop-out is the viewer alone: it needs the passkey gate's state
  // (reachability + whether a passkey is set up) and nothing else, so none of
  // the panel fetches or polls below start in it.
  if (screenWindow) {
    await Promise.all([safe(fetchStatus), safe(fetchWebauthnStatus)]);
    openScreenWindow();
    return;
  }

  // A PC mirror (?terminal=) or human-shared link (?session=) drops straight
  // into the same session; only the former set state.isMirrorWindow above.
  // ?board=<sid> lands a Slack ping on that session's Board
  // card, drawer open — mutually exclusive with the session links by
  // construction (each link carries one param).
  const deepLinkSid = mirrorSid || sharedSessionSid;
  const boardSid = deepLinkSid ? null : consumeUrlParam('board');
  if (deepLinkSid) startSessionViews();

  // Every panel fetch starts at once: awaited one by one they cost
  // the phone ~13 serial round trips over the tunnel before the git flags
  // (last in line) painted. Only real reads are chained. The session and
  // project lists render agent icons from state.agents, and fetchAgents
  // re-renders neither, so both wait for it.
  const lists = safe(fetchAgents).then(function () {
    return Promise.all([safe(fetchApps), safe(fetchSessions)]);
  }).then(function () {
    // A Board opened before the project list landed drew its issue actions
    // without it (they key on the registered projects). git-status used to
    // redraw it by coming last in the serial chain; now nothing else would.
    if (state.tab === 'board') renderBoard();
  });
  // The Team OS skills fill their tab's list and header count; on any other
  // tab this asks nothing, and the tab's first visit reads them.
  const skills = safe(fetchSkills);
  // The terminal deep link reads both: reachability and the passkey gate.
  const status = safe(fetchStatus);
  const webauthn = safe(fetchWebauthnStatus);
  // Git flags fill without a tap: one fetch at boot when the Coding tab (the
  // only view that shows them) is the one open, then the slow poll below keeps
  // them current while it is. Any other tab asks nothing; opening Coding does.
  const git = safe(function () {
    return state.tab === 'claude' ? refreshGitStatus({ quiet: true }) : Promise.resolve();
  });
  const listeners = safe(fetchListeners);
  const running = safe(fetchRunningApps);
  const load = safe(fetchSystemLoad);
  // The readout's restart sheet reads /api/restart once; no targets, no button.
  const others = [safe(fetchVersion), safe(function () {
    return import('./shared/build-restart.js').then(function (m) { return m.wireBuildRestart(els.buildReadout); });
  })];
  // The Board card link fetches the Board itself, so it needs none of these.
  if (boardSid) openBoardCard(boardSid).catch(function () {});
  // A reload restores the last tab without a tap, and the Board and Jobs
  // read their data only on a tap or their poll: the restored tab would
  // stay empty for a whole poll interval. Both self-gate to their tab.
  else others.push(safe(fetchBoard), safe(fetchJobs));

  armPolls({ lists, listeners, running, webauthn, load, git });
  lists.then(function () { loadAfterList(status); });

  if (deepLinkSid) {
    await Promise.all([lists, status, webauthn]);
    const found = state.sessions.find(function (s) {
      return s.session_id === deepLinkSid;
    });
    openTerminal(found || { session_id: deepLinkSid, name: deepLinkSid });
  }
  // safe() never rejects, so neither does this: boot() resolves once every
  // boot fetch has settled.
  await Promise.all([lists, skills, status, webauthn, git, listeners, running, load].concat(others));
}

// What loads once the list has drawn: the views a tap opens and the terminal's
// scripts (lazy-views.js; `status` says whether the terminal is reachable),
// and what no first screen draws: the copy button of rendered Markdown, which
// only a lazily loaded view renders, the new-build prompt, which checks every
// few minutes, and the Usage card, whose read is answered from server memory
// so it can never hold up the list.
function loadAfterList(status) {
  prefetchViews(status);
  Promise.all([import('./quote-copy.js'), import('./_vendored/build-watch/build-watch.js')])
    .then(function (m) {
      m[0].wireQuoteCopy();
      m[1].initBuildWatch();
    })
    .catch(function (exc) { console.warn('boot: a deferred module did not load', exc); });
  import('./copilot-usage.js')
    .then(function (mod) { mod.wireCopilotUsage(); })
    .catch(function (exc) { console.warn('usage card did not load', exc); });
}

// Cache the URL's tokens and read its session links: ?terminal=<sid> (the
// launcher-spawned PC mirror) and ?session=<sid> (a shared link).
function consumeSessionParams() {
  const fromUrl = consumeUrlParam('token');
  if (fromUrl) writeToken(fromUrl);
  // A launcher-spawned PC mirror window on the ts.net URL carries a
  // server-minted passkey terminal token — cache it like a
  // ceremony-minted one. TTL mirrors the server's 12 h _TERMINAL_TOKEN_TTL.
  const ttFromUrl = consumeUrlParam('tt');
  if (ttFromUrl) writeTerminalToken(ttFromUrl, 12 * 3600);
  const mirrorSid = consumeUrlParam('terminal');
  const sharedSessionSid = consumeUrlParam('session');
  // Only the launcher-spawned PC mirror window uses ?terminal=<sid>.
  // Human-copyable links use ?session=<sid>, so opening one on a phone does
  // not claim mirror ownership or surrender the phone's PTY-size authority.
  // Recording only the former here is what lets terminal.js tell a real
  // mirror apart from an ordinary browser.
  state.isMirrorWindow = !!mirrorSid;
  // Mark the window closable before the first network call: a mirror whose
  // /api/config 401s returns from boot() and never reaches announceMirrorWindow
  // ?session= links stay unmarked.
  if (mirrorSid) markMirrorWindowEarly(mirrorSid);
  return { mirrorSid, sharedSessionSid };
}

// Each poll arms once its own first fetch has settled, so it never
// overlaps it, and never waits on a slower, unrelated one: a git scan
// still running must not hold back the Jobs poll. A second boot (a sign-in
// after a 401) arms nothing more: the polls already running carry on.
let pollsArmed = false;
// Whether a hidden tab stops every page poll: the Settings > Performance switch.
const HIDDEN_POLICY = { pauseHidden: pauseWhenHidden };
function armPolls(first) {
  if (pollsArmed) return;
  pollsArmed = true;
  first.lists.then(function () {
    startPoll(function () { return fetchApps(); }, TUNNEL_POLL_MS, HIDDEN_POLICY);
    startPoll(function () {
      // Pause the session poll while the session overlay is open (either
      // mode) — it would re-render the list under the overlay for no
      // reason. The open terminal keeps its own title poll (terminal.js).
      // Not on the wide layout: the view docks beside the list
      // so the list is on screen and must keep updating — a session
      // that ends there would otherwise keep its card until a reload.
      if (!state.sessionView || isWideLayout()) return fetchSessions();
    }, pollMs('poll_sessions_s', SESSIONS_POLL_MS), HIDDEN_POLICY);
  });
  first.listeners.then(function () {
    startPoll(function () { return fetchListeners(); }, LISTENERS_POLL_MS, HIDDEN_POLICY);
  });
  first.running.then(function () {
    // fetchRunningApps() self-gates: it no-ops unless the Apps tab is up.
    startPoll(function () { return fetchRunningApps(); }, RUNNING_APPS_POLL_MS, HIDDEN_POLICY);
  });
  first.webauthn.then(function () {
    startPoll(function () { return fetchWebauthnStatus(); }, WEBAUTHN_POLL_MS, HIDDEN_POLICY);
  });
  first.load.then(function () {
    // Every tab's header shows it.
    startPoll(function () { return fetchSystemLoad(); }, SYSTEM_LOAD_POLL_MS, HIDDEN_POLICY);
  });
  first.git.then(function () {
    startPoll(function () {
      // Always-on git flags: refresh only while the tab that shows
      // them (Code; the Board's Backlog doesn't draw them) is up.
      if (state.tab !== 'claude') return;
      return refreshGitStatus({ quiet: true });
    }, GIT_STATUS_POLL_MS, HIDDEN_POLICY);
    // Coming back to Coding reads at once: the server answers from its cache
    // (stale or fresh, labelled) and refreshes it, so the tab never waits on git.
    document.addEventListener('launcher:tab', function (ev) {
      if (ev.detail && ev.detail.tab === 'claude' && !document.hidden) {
        refreshGitStatus({ quiet: true });
      }
    });
  });
  // No boot fetch of their own: both self-gate to their tab.
  startPoll(function () { return fetchJobs(); }, JOBS_POLL_MS, HIDDEN_POLICY);
  startPoll(function () { return fetchBoard(); }, pollMs('poll_board_s', BOARD_POLL_MS), HIDDEN_POLICY);
}

// --------------------------------------------------------- wire + go
// The tab a boot opens on decides which view code main.js evaluates before
// its wiring (the same test as index.html's preload script, which already
// asked for those modules). A boot that reopens on the Board, or lands on a
// ?board= link, evaluates the Board's code; every other boot but the Screen
// pop-out evaluates the Code and Apps tabs' code, which a Board-first boot
// loads after the lanes draw. Each is wired where it was wired when it was
// part of this graph: after wireTabs(), whose tab click must run before
// theirs. Handing the modules over directly skips an import() whose
// resolution would queue behind boot()'s other tasks.
function bootView() {
  const params = new URLSearchParams(window.location.search);
  if (params.has('screen')) return 'screen';
  if (params.has('terminal') || params.has('session')) return 'session';
  if (params.has('board')) return 'board';
  try { return localStorage.getItem('app-launcher.tab') === 'board' ? 'board' : 'code'; } catch (e) { return 'code'; }
}
const view = bootView();
// A failed load is retried, and reported, by the view's first use.
const [boardFirst, appsFirst] = await Promise.all([
  view === 'board' ? import('./board.js').catch(function () { return null; }) : null,
  view === 'board' || view === 'screen' ? null
    : Promise.all([
      import('./apps.js'), import('./apps/apps-coding.js'), import('./apps/apps-listeners.js'),
    ]).then(function (m) {
      return { apps: m[0], coding: m[1], listeners: m[2] };
    }, function () { return null; }),
]);

setSignInHandler(boot);
wireUsageLog();
wireTabs();
wireClaudeOptions();
wireSessions();
wireSystemLoad();
if (appsFirst) adoptApps(appsFirst);
openDesktopColumns();
if (boardFirst) adoptBoard(boardFirst);
wireViewOpeners();
wireWebauthn();
wireSettings();
wireTheme();

boot();
