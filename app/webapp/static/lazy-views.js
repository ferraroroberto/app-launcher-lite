/* The views a tap opens load on that first tap, not with the page.
 *
 * The chat, terminal, changes, links, Screen and Progress code, the Jobs
 * tab, the Board's answer sheet and the shared composer are most of the
 * app's JavaScript, and none of it draws the shell, the session list or the
 * Board. A phone with an empty cache (a first visit, or any visit after
 * a deploy, since one hash stamps every module URL) downloaded all of it
 * before either could draw. Each group below loads and wires its listeners
 * once: on the first call that needs it, or from prefetchViews() once the
 * page has drawn, so a later first open finds it already parsed.
 *
 * Nothing in the first-load graph imports these modules statically; every
 * caller goes through the functions here.
 */

import { state } from './state.js';
import { opensInPcWindow, toast } from './api.js';
import { fetchSessions } from './sessions.js';

// A group loads once; a failed load (a dropped connection) is retried by
// the next call instead of staying failed.
function group(load) {
  let pending = null;
  let loaded = null;
  const get = function () {
    if (!pending) {
      pending = load().then(function (mods) {
        loaded = mods;
        return mods;
      }, function (exc) {
        pending = null;
        throw exc;
      });
    }
    return pending;
  };
  get.loaded = function () { return loaded; };
  return get;
}

// The overlay holds the terminal and the chat pane, and both reach the
// changes overlay and the links sheet, so the six load as one. They wire in
// the order main.js wired them when they were part of the first load.
const sessionViews = group(function () {
  return Promise.all([
    import('./terminal.js'),
    import('./session-overlay.js'),
    import('./chat/session-transcript.js'),
    import('./changes/changes-overlay.js'),
    import('./links/links-sheet.js'),
    import('./terminal/terminal-mirror.js'),
  ]).then(function (m) {
    const views = { terminal: m[0], overlay: m[1], chat: m[2], changes: m[3], links: m[4], mirror: m[5] };
    views.terminal.wireTerminal();
    views.chat.wireChatPane();
    views.changes.wireChanges();
    views.links.wireLinks();
    return views;
  });
});

const screenView = group(function () {
  return import('./screen/screen-view.js').then(function (m) {
    m.wireScreenView();
    return m;
  });
});

// The Jobs tab loads on its first visit (or at boot when the app reopens on
// it); until then nothing reads or polls jobs. A search typed before it
// landed runs once it has.
const jobsTab = group(function () {
  return import('./jobs.js').then(function (m) {
    m.wireJobs();
    const search = document.getElementById('jobsSearchInput');
    if (search && search.value) search.dispatchEvent(new Event('input'));
    return m;
  });
});

// The Board's answer sheet. The poll's last chief run state is replayed on
// load, so a sheet opened later gates Done on it as before.
let lastChiefRun;
const answerSheet = group(function () {
  return import('./board/board-answers.js').then(function (m) {
    m.wireChiefAnswers();
    if (lastChiefRun !== undefined) m.syncChiefAnswers(lastChiefRun);
    return m;
  });
});

// The shared composer, its mic and the session upload path, for the Board
// drawer's reply box.
const composerKit = group(function () {
  return Promise.all([
    import('./composer.js'),
    import('./voice.js'),
    import('./terminal/terminal-compose.js'),
  ]).then(function (m) {
    return {
      mountComposer: m[0].mountComposer,
      voiceDictationAvailable: m[1].voiceDictationAvailable,
      uploadSessionFile: m[2].uploadSessionFile,
    };
  });
});

const progressSheet = group(function () {
  return import('./progress/progress-sheet.js').then(function (m) {
    m.wireProgressSheet();
    return m;
  });
});

// Run `fn` on a group's modules. A loaded group runs it synchronously, still
// inside the tap that asked: a pop-up window or a passkey prompt needs
// that gesture, and an await in between can lose it.
function use(get, fn) {
  const ready = get.loaded();
  if (ready) return Promise.resolve(fn(ready));
  return get().then(fn, function (exc) {
    console.warn('lazy-views: a view failed to load', exc);
    toast('Could not load this view. Check the connection and try again.', 'error');
  });
}

export function openTerminal(session) {
  return use(sessionViews, function (v) { return v.terminal.openTerminal(session); });
}

export function openSessionOverlay(session, wantMode) {
  return use(sessionViews, function (v) { return v.overlay.openSessionOverlay(session, wantMode); });
}

// Nothing can be open before the session views have loaded.
export function closeSessionOverlay() {
  const views = sessionViews.loaded();
  if (views) views.overlay.closeSessionOverlay();
}

export function openLinks(session) {
  return use(sessionViews, function (v) { return v.links.openLinks(session); });
}

export function openChanges(project) {
  return use(sessionViews, function (v) { return v.changes.openChanges(project); });
}

// The PC mirror window titles itself before its first network call. Calls
// queued on the same load run in order, so this lands before the deep
// link's openTerminal.
export function markMirrorWindowEarly(sid) {
  return use(sessionViews, function (v) { return v.mirror.markMirrorWindowEarly(sid); });
}

// The post-launch tail shared by every PTY-launch response handler (Coding
// tab's launchApp, Team OS's skill launch): refresh the sessions list, then
// drop straight into the in-page terminal for a full-control (non-'remote')
// session on a phone — a desktop browser already got its dedicated PC Edge
// window instead (the setting is on), so it stays on the SPA. No-op when
// the launch produced no session (a non-claude-code app launch).
export function handleLaunchResponse(session) {
  if (!session) return;
  fetchSessions().catch(function () {});
  if (session.kind !== 'remote' && !opensInPcWindow()) openTerminal(session);
}

// Self-gated to the Jobs tab, like the module's own fetchJobs: the poll
// and boot call it on every tab, and only a Jobs visit loads the tab.
export function fetchJobs() {
  const jobs = jobsTab.loaded();
  if (jobs) return jobs.fetchJobs();
  if (state.tab !== 'jobs') return Promise.resolve();
  return use(jobsTab, function (m) { return m.fetchJobs(); });
}

export function openChiefAnswers(plan, run, shown, startAt) {
  return use(answerSheet, function (m) { return m.openChiefAnswers(plan, run, shown, startAt); });
}

export function openOwnerAnswers(plan, run, startRef, kind) {
  return use(answerSheet, function (m) { return m.openOwnerAnswers(plan, run, startRef, kind); });
}

export function syncChiefAnswers(run) {
  lastChiefRun = run;
  const sheet = answerSheet.loaded();
  if (sheet) sheet.syncChiefAnswers(run);
}

// Runs `fn` with the composer kit; synchronously once it has loaded, so a
// re-rendered drawer mounts its reply box in the same frame.
export function withComposer(fn) {
  return use(composerKit, fn);
}

// The pop-out window's boot path (main.js, ?screen=1).
export function openScreenWindow() {
  return use(screenView, function (m) { return m.openScreenWindow(); });
}

// The openers that sit outside their own view: Progress's History button,
// the PC load sheet's Screen row, and a first visit to the Jobs tab.
export function wireViewOpeners() {
  document.addEventListener('launcher:tab', function (ev) {
    if (ev.detail && ev.detail.tab === 'jobs' && !jobsTab.loaded()) fetchJobs();
  });
  // The Jobs pane draws before its code lands, so a tap that beats the code
  // is held and replayed once it has, never dropped. Held in the capture
  // phase, it reaches the usage log once: as the replay. The page head's
  // buttons (theme, Settings, load) are wired at boot and pass straight on.
  const jobsPane = document.getElementById('paneJobs');
  if (jobsPane) {
    jobsPane.addEventListener('click', function (ev) {
      if (jobsTab.loaded()) return;
      const control = ev.target.closest('button, a[href], summary, [role="button"], [role="tab"]');
      if (!control || control.closest('.page-head')) return;
      ev.preventDefault();
      ev.stopPropagation();
      use(jobsTab, function () { control.click(); });
    }, true);
  }
  const history = document.getElementById('historyOpen');
  if (history && document.getElementById('progressSheet')) {
    history.addEventListener('click', function () {
      use(progressSheet, function (m) { return m.openProgressSheet(); });
    });
  }
  const screenRow = document.getElementById('cpuSheetScreen');
  if (screenRow && document.getElementById('screenView')) {
    screenRow.addEventListener('click', function () {
      const sheet = document.getElementById('cpuSheet');
      if (sheet && sheet.open) sheet.close();
      use(screenView, function (m) { return m.openScreen(); });
    });
  }
}

// The session views start loading at once for a deep link that opens one.
export function startSessionViews() {
  prefetch(sessionViews);
}

// Once the list has drawn, load every view, so the first tap on one opens
// as fast as it did when they loaded with the page. A session row is the tap
// that comes first, so its views start at once; the rest wait for the phone
// to go idle. Safari has no requestIdleCallback; a short timer stands in.
const PREFETCH_IDLE_TIMEOUT_MS = 3000;
const PREFETCH_FALLBACK_MS = 300;

function prefetch(get) {
  get().catch(function () { /* the first open retries and reports it */ });
}

export function prefetchViews() {
  prefetch(sessionViews);
  const run = function () {
    [composerKit, answerSheet, jobsTab, screenView, progressSheet].forEach(prefetch);
  };
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(run, { timeout: PREFETCH_IDLE_TIMEOUT_MS });
  } else {
    setTimeout(run, PREFETCH_FALLBACK_MS);
  }
}
