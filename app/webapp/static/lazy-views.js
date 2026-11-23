/* The views a tap opens load on that first tap, not with the page.
 *
 * The chat, terminal, changes, links, Screen and Progress code, the Jobs,
 * Team OS and Board tabs, the Code and Apps tabs' drawing on a boot that
 * reopens on the Board, the Settings pane and the shared composer are most
 * of the app's JavaScript, and none of it draws the shell or the session
 * list. A phone with an empty
 * cache (a first visit, or any visit after a deploy, since one hash stamps
 * every module URL) downloaded all of it before either could draw. Each
 * group below loads and wires its listeners once: on the first call that
 * needs it, or from prefetchViews() once the page has drawn, so a later
 * first open finds it already parsed.
 *
 * Nothing in the first-load graph imports these modules statically; every
 * caller goes through the functions here.
 */

import { state } from './state.js';
import { opensInPcWindow, toast } from './api.js';
import { fetchSessions } from './sessions.js';
import { readApps } from './apps/apps-reads.js';

// A group loads once; a failed load (a dropped connection) is retried by
// the next call instead of staying failed.
function group(load, adoptWire) {
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
  // A module the caller already evaluated: wire it now, with no import()
  // hop, whose resolution waits behind the boot's other tasks.
  get.adopt = function (mod) {
    if (pending) return;
    loaded = adoptWire(mod);
    pending = Promise.resolve(loaded);
  };
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

// The Board tab loads on its first visit, or once the phone goes idle; a
// boot that reopens on it, or lands on a ?board= link, hands over the module
// main.js evaluated (adoptBoard). Text typed into its project filter before
// it landed is replayed, as Jobs' search is.
function wireBoardModule(m) {
  m.wireBoard();
  const repo = document.getElementById('boardDispatchRepoInput');
  if (repo && repo.value) repo.dispatchEvent(new Event('input'));
  return m;
}
const boardTab = group(function () {
  return import('./board.js').then(wireBoardModule);
}, wireBoardModule);

// The Code and Apps tabs' drawing: the project list, its toolbar and git
// sync, and the Apps, Trays, Running and Other ports cards. Any boot but a
// Board-first one hands over the modules main.js evaluated before its wiring
// (adoptApps); a Board-first boot loads them after the lanes draw, or on the
// first visit to either tab. The registry and agent reads the Board needs run without them
// (apps/apps-reads.js), and loading draws what those reads brought.
function wireAppsModules(m) {
  m.apps.wireApps();
  return m;
}
const appsView = group(function () {
  return Promise.all([
    import('./apps.js'),
    import('./apps/apps-coding.js'),
    import('./apps/apps-listeners.js'),
  ]).then(function (m) {
    return wireAppsModules({ apps: m[0], coding: m[1], listeners: m[2] });
  });
}, wireAppsModules);

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

// A Board card's drill-down drawer loads with the first card opened, or once
// the phone goes idle, never with the Board: its lanes draw without it.
const boardDrawer = group(function () {
  return import('./board/drawer.js');
});

// The note sheet a Chief's plan row opens.
const planNoteSheet = group(function () {
  return import('./board/plan-note-sheet.js');
});

// The Board's "Since you last looked" card: it draws after its own read, so
// its code loads with the Board's first read, not before the lanes draw.
const journalCard = group(function () {
  return import('./board/chief-journal.js').then(function (m) {
    m.wireJournal();
    return m;
  });
});

// The Apps tab's rename and scan dialogs: only a tap opens either.
const appsDialogs = group(function () {
  return import('./apps/apps-dialogs.js').then(function (m) {
    m.wireRenameDialog();
    m.wireScanDialog();
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

// The Settings pane's code: its sheets, fields and rows. The page loads on
// the Code or Board tab, and Settings opens from a header's gear, so none of
// it draws the first screen. Loaded, it draws the config the boot already
// read, and a load that lands during a Settings visit reads what a visit
// reads (the token count, the documents list), whose own tab listeners
// were not yet attached when the visit began.
const settingsPane = group(function () {
  return Promise.all([
    import('./settings/settings-sheets.js'),
    import('./settings/performance.js'),
    import('./settings/lid-keep-awake.js'),
    import('./settings/install-readiness.js'),
    import('./settings/diagnostics.js'),
    import('./settings/tokens.js'),
    import('./settings/documents.js'),
    import('./_vendored/fullscreen/fullscreen.js'),
    import('./_vendored/text-size/text-size.js'),
    import('./settings/claude-options.js'),
    import('./settings/speech-settings.js'),
    import('./settings/network-access.js'),
    import('./settings/mirror-browser.js'),
    import('./settings/launch-presets-settings.js'),
  ]).then(function (m) {
    const s = { sheets: m[0], performance: m[1], lid: m[2], speech: m[10], network: m[11], mirror: m[12], presets: m[13] };
    const fetchConfig = m[9].fetchConfig;
    s.network.wireNetworkAccess(fetchConfig);
    s.presets.wireLaunchPresets(m[9].savePresets);
    s.lid.wireLidKeepAwake(function () { return fetchConfig(); });
    // Settings > Full screen on Edge: the row stays hidden in any other
    // browser. Same localStorage prefix as the theme and text size.
    m[7].bindFullscreen(document.getElementById('fullscreenRow'), 'app-launcher');
    m[3].renderInstallReadiness();
    m[4].wireDiagnostics();
    s.performance.wirePerformance();
    s.sheets.wireSettingsSheets();
    m[5].wireTokens();
    m[6].wireDocuments();
    // Settings > Text size: the pre-paint boot script reads the same key.
    const textSize = document.getElementById('textSizeControl');
    if (textSize) m[8].bindTextSize(textSize, 'app-launcher');
    if (state.config) {
      s.lid.renderLidKeepAwake(state.config);
      s.performance.renderPerformance(state.config);
      s.speech.renderSpeech(state.config);
      s.mirror.renderMirrorBrowser(state.config);
      s.network.renderNetworkAccess(state.config);
      s.presets.renderLaunchPresetsEditor();
    }
    s.sheets.renderSettingsValues();
    if (state.tab === 'settings') {
      m[5].ensureLoaded();
      m[6].fetchList(true);
    }
    return s;
  });
});

// The Team OS tab loads on its first visit, or once the phone goes idle; a
// boot that reopens on it loads it from the boot's skills read. A filter
// typed before it landed is replayed, as Jobs' search is.
const teamOsTab = group(function () {
  return import('./team-os.js').then(function (m) {
    m.wireTeamOs();
    const filter = document.getElementById('teamOsFilterInput');
    if (filter && filter.value) filter.dispatchEvent(new Event('input'));
    return m;
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

// Self-gated to the Board tab like fetchJobs above: only a visit loads it.
export function fetchBoard() {
  const board = boardTab.loaded();
  if (board) return board.fetchBoard();
  if (state.tab !== 'board') return Promise.resolve();
  return use(boardTab, function (m) { return m.fetchBoard(); });
}

// The registry read: drawn by the Code and Apps tabs once their code has
// loaded, read alone before (the Board keys its issue actions on it). Code
// that lands while a lone read is in flight draws that read when it ends.
export function fetchApps() {
  const views = appsView.loaded();
  if (views) return views.apps.fetchApps();
  return readApps().then(function () {
    const landed = appsView.loaded();
    if (landed) landed.apps.showAppsRead();
  });
}

// Self-gated to the Apps tab, like the module's own fetchRunningApps.
export function fetchRunningApps() {
  const views = appsView.loaded();
  if (views) return views.apps.fetchRunningApps();
  if (state.tab !== 'apps') return Promise.resolve();
  return use(appsView, function (m) { return m.apps.fetchRunningApps(); });
}

// Self-gated to the Apps tab like the module's own fetchListeners: a visit
// loads the code and reads.
export function fetchListeners() {
  const views = appsView.loaded();
  return views ? views.listeners.fetchListeners() : Promise.resolve();
}

// Only the Code tab draws git flags: the boot, the poll and a visit call it
// on that tab alone, so a call before the code has loaded loads it.
export function refreshGitStatus(options) {
  const views = appsView.loaded();
  if (views) return views.coding.refreshGitStatus(options);
  if (state.tab !== 'claude') return Promise.resolve();
  return use(appsView, function (m) { return m.coding.refreshGitStatus(options); });
}

// main.js's boot on any tab but the Board, once the tabs are wired.
export function adoptApps(mods) {
  appsView.adopt(mods);
}

// Nothing is drawn before the Board's code has loaded.
export function renderBoard() {
  const board = boardTab.loaded();
  if (board) board.renderBoard();
}

// The ?board=<sid> deep link (main.js).
export function openBoardCard(sid) {
  return use(boardTab, function (m) { return m.openBoardCard(sid); });
}

// main.js's Board-first boot, once the tabs are wired.
export function adoptBoard(mod) {
  boardTab.adopt(mod);
}

// The drawer module once loaded, else null: until then no drawer is open.
export function boardDrawerLoaded() {
  return boardDrawer.loaded();
}

// Resolves true once the drawer's code has loaded, false when it failed (the
// toast has said so).
export function loadBoardDrawer() {
  return use(boardDrawer, function () { return true; }).then(function (ok) { return ok === true; });
}

export function openRename(a) {
  return use(appsDialogs, function (m) { return m.openRename(a); });
}

export function scanApps() {
  return use(appsDialogs, function (m) { return m.runScan(); });
}

export function openPlanNoteSheet(o) {
  return use(planNoteSheet, function (m) { return m.openPlanNoteSheet(o); });
}

// Quiet like the card itself: a load that fails waits for the next poll.
export function pollJournal() {
  return journalCard().then(function (m) { return m.pollJournal(); }, function () {});
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

// Every config read redraws Settings; before its code has loaded there is
// nothing to redraw, and the load draws the config itself.
export function renderSettingsValues() {
  const s = settingsPane.loaded();
  if (s) s.sheets.renderSettingsValues();
}

export function renderPerformance(cfg) {
  const s = settingsPane.loaded();
  if (s) s.performance.renderPerformance(cfg);
}

export function renderLidKeepAwake(body) {
  const s = settingsPane.loaded();
  if (s) s.lid.renderLidKeepAwake(body);
}

export function renderSpeech(cfg) {
  const s = settingsPane.loaded();
  if (s) s.speech.renderSpeech(cfg);
}

export function renderMirrorBrowser(body) {
  const s = settingsPane.loaded();
  if (s) s.mirror.renderMirrorBrowser(body);
}

export function renderNetworkAccess(body) {
  const s = settingsPane.loaded();
  if (s) s.network.renderNetworkAccess(body);
}

export function renderLaunchPresetsEditor() {
  const s = settingsPane.loaded();
  if (s) s.presets.renderLaunchPresetsEditor();
}

// tabs.js's openSettingsAt: an empty state's fix, or the Board's chief gear.
export function revealSettingsField(el) {
  return use(settingsPane, function (s) { return s.sheets.revealSettingsField(el); });
}

// Self-gated to the Team OS tab like fetchJobs: the boot and a Settings
// folder change call it on every tab, and only a visit loads the tab.
export function fetchSkills() {
  const team = teamOsTab.loaded();
  if (team) return team.fetchSkills();
  if (state.tab !== 'teamos') return Promise.resolve();
  return use(teamOsTab, function (m) { return m.fetchSkills(); });
}

// The tab builds its model list when it loads; until then there is none.
export function setTeamOsModelOptions() {
  const team = teamOsTab.loaded();
  if (team) team.setTeamOsModelOptions();
}

// A tab's pane draws before its code lands, so a tap that beats the code is
// held and replayed once it has, never dropped. Held in the capture phase,
// it reaches the usage log once: as the replay. The page head's buttons
// (theme, Settings, load) are wired at boot and pass straight on.
function holdPaneTaps(selector, get) {
  const pane = document.querySelector(selector);
  if (!pane) return;
  pane.addEventListener('click', function (ev) {
    if (get.loaded()) return;
    const control = ev.target.closest('button, a[href], summary, [role="button"], [role="tab"]');
    if (!control || control.closest('.page-head')) return;
    ev.preventDefault();
    ev.stopPropagation();
    use(get, function () { control.click(); });
  }, true);
}

// The openers that sit outside their own view: Progress's History button,
// the PC load sheet's Screen row, and a first visit to the Jobs, Board, Code
// or Apps tab.
export function wireViewOpeners() {
  document.addEventListener('launcher:tab', function (ev) {
    const tab = ev.detail && ev.detail.tab;
    if (tab === 'jobs' && !jobsTab.loaded()) fetchJobs();
    // Once loaded, board.js's own tab listener runs the visit.
    if (tab === 'board' && !boardTab.loaded()) {
      use(boardTab, function (m) { m.openBoardTab(); });
    }
    // Once loaded, team-os.js's own tab-click listener reads on each visit.
    if (tab === 'teamos' && !teamOsTab.loaded()) {
      use(teamOsTab, function (m) {
        m.fetchSkills().catch(function () {});
        m.fetchRecapStatus().catch(function () {});
      });
    }
    if (tab === 'settings' && !settingsPane.loaded()) prefetch(settingsPane);
    // Once loaded, apps.js's own Apps tab listener reads on each visit; the
    // Code tab's git read is main.js's tab listener, through refreshGitStatus.
    if (tab === 'apps' && !appsView.loaded()) {
      use(appsView, function (m) {
        m.apps.fetchRunningApps().catch(function () {});
        m.listeners.fetchListeners().catch(function () {});
      });
    }
    if (tab === 'claude' && !appsView.loaded()) prefetch(appsView);
  });
  holdPaneTaps('#paneJobs', jobsTab);
  holdPaneTaps('#paneBoard', boardTab);
  holdPaneTaps('#paneTeamOS', teamOsTab);
  holdPaneTaps('#paneSettings', settingsPane);
  holdPaneTaps('#paneApps', appsView);
  // The Code tab's session list draws without that code; its projects card
  // does not.
  holdPaneTaps('#paneClaude .projects-card', appsView);
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
// On the Board the open tab's own code comes first: the list can draw before
// it lands, and the session views would share the link with it. The Code and
// Apps tabs' code, which a Board-first boot left out, leads the idle loads.
const PREFETCH_IDLE_TIMEOUT_MS = 3000;
const PREFETCH_FALLBACK_MS = 300;

function prefetch(get) {
  return get().catch(function () { /* the first open retries and reports it */ });
}

export function prefetchViews() {
  const first = state.tab === 'board' ? prefetch(boardTab) : Promise.resolve();
  first.then(function () { prefetch(sessionViews); });
  const run = function () {
    [appsView, boardTab, boardDrawer, planNoteSheet, appsDialogs, composerKit, answerSheet, jobsTab, teamOsTab, settingsPane, screenView,
      progressSheet].forEach(prefetch);
  };
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(run, { timeout: PREFETCH_IDLE_TIMEOUT_MS });
  } else {
    setTimeout(run, PREFETCH_FALLBACK_MS);
  }
}
