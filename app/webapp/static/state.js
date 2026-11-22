/* Shared singletons: app state, DOM-element references, constants.
 *
 * State (mutable):
 *   state.tab          — 'claude' | 'apps' | 'jobs' | 'teamos' | 'board' | 'settings'
 *   state.config       — /api/config payload (Copilot flags + scan paths)
 *   state.apps         — array from /api/apps (each entry carries its own .health)
 *   state.agents       — array from /api/agents ({id,label,available} per agent)
 *   state.runningApps  — array from /api/apps/running (launcher-spawned apps)
 *   state.listeners    — array from /api/ports/probe; null until the first read
 *   state.sessions     — array from /api/claude-code/sessions
 *   state.pendingScan  — array from /api/apps/scan, surfaced in scan dialog
 *   state.webauthn     — { configured, enrollment_open, devices[] }
 *   state.terminal     — null when overlay closed, else { sid, ws, term, fit, onWindowResize }
 *   state.status       — /api/status payload (incl. terminal reachability)
 *
 * Auth: a bearer token is stored in localStorage. The page extracts it
 * from ?token=… on first load and strips it from the URL. On 401, the
 * sign-in sheet opens; password → /api/login → bearer token.
 */

export const TOKEN_KEY = 'launcher.token';
export const TT_KEY = 'launcher.tt';
export const TT_EXP_KEY = 'launcher.tt.exp';

export const TUNNEL_POLL_MS = 4000;       // refresh tunnel-kind URLs + health
export const SESSIONS_POLL_MS = 5000;     // refresh running Claude Code sessions
export const LISTENERS_POLL_MS = 5000;    // refresh port listeners
export const RUNNING_APPS_POLL_MS = 4000; // refresh launcher-spawned apps
export const JOBS_POLL_MS = 4000;         // refresh Jobs tab while it's visible
export const BOARD_POLL_MS = 5000;        // refresh Board tab while it's visible
export const WEBAUTHN_POLL_MS = 15000;
// The header's CPU reading: one poll for the whole page, foreground
// only; the server samples every 5 s, so 10 s never reads the same sample
// twice in a row by much. The retry covers the sampler's ~2 s first sample.
export const SYSTEM_LOAD_POLL_MS = 10000;
export const SYSTEM_LOAD_WARMING_RETRY_MS = 3000;
// Git-status refresh (not tap-only): slow on
// purpose — one git run per project per tick, gated to the tabs that show
// the flags (Coding tiles, Board backlog) and to a foreground page.
export const GIT_STATUS_POLL_MS = 45000;

export const state = {
  tab: 'claude',
  config: null,
  apps: [],
  // Whether a mini apps folder is configured (the Mini apps fold's gate).
  miniAppsEnabled: false,
  // Coding agents — overwritten by /api/agents at boot. This fork runs one,
  // GitHub Copilot CLI; the fallback keeps the Coding tab usable if that
  // fetch fails, assuming it present. The fullscreen flag mirrors
  // src/agents.py so a terminal opened while the fetch is degraded still
  // takes the pan-not-reflow path a full-screen TUI needs.
  agents: [
    { id: 'copilot', label: 'GitHub Copilot CLI', available: true, fullscreen: true },
  ],
  // Is the `code` CLI on the server's PATH? Drives the enabled
  // state of the Coding row's VS Code button. Overwritten by the same
  // /api/agents boot fetch as `agents`; conservative `false` until it lands,
  // matching how a not-yet-detected agent renders — a button that greys in
  // for a moment beats one that offers a launch the server would 503.
  vscodeAvailable: false,
  runningApps: [],
  // Every port listener on this PC: the Other ports sheet and its
  // row's count. null until the first probe lands, so the row can say
  // "Checking…" instead of a count it does not have.
  listeners: null,
  // Git flags for the Coding tiles + Board backlog (always-on). null only until the boot fetch lands; then a map of
  // project id → { is_git, branch, default_branch, on_default_branch,
  // dirty }, refreshed by the GIT_STATUS_POLL_MS poll while the Coding or
  // Board tab is visible. Cached: the 4-5 s tab polls re-render from this
  // map but never re-run the git check themselves.
  gitStatus: null,
  // When that map last landed (Date.now()), for the Projects card's "Git
  // checked N min ago" footer. null until the first fetch.
  gitStatusAt: null,
  // Coding-tab favorites filter. false = show all projects
  // (favorites pinned to the top); true = show only starred projects. A
  // client-side view toggle, persisted across reloads so the
  // 4 s apps poll re-renders without dropping it.
  codingFavFilter: localStorage.getItem('launcher.codingFavFilter') === '1',
  jobs: [],
  jobsLoaded: false,   // first /api/jobs answer arrived
  // Jobs-list ordering. 'next' = ascending by computed next
  // fire (imminent dailies above weeklies; manual/paused sink to the
  // bottom); 'name' = A–Z. Persisted across reloads.
  jobsSort: localStorage.getItem('launcher.jobsSort') === 'name' ? 'name' : 'next',
  jobsSearchQuery: '',
  jobsSearchMatches: [],
  jobRuns: {},      // job_id → array of recent runs (lazy)
  sheetJob: null,    // job_id whose sheet is open (the inline accordion before)
  selectedRun: null, // { jobId, runId } — which run's log is in the sheet
  sessions: [],
  // Board tab: the /api/board payload, null until the
  // tab's first fetch. The 5 s poll self-gates on the tab being visible;
  // GitLab data inside it comes from the server-side glab cache and is only
  // refreshed on demand (the ↻ button / first activation), never per poll.
  board: null,
  // When `board` last landed (Date.now()): the Board tab's nav badge counts
  // from it while it is this fresh, else from the sessions poll.
  boardFetchedAt: 0,
  // GET /api/board/chief-plan's last answer, fetched with `board`.
  chiefPlan: null,
  // The chief's unanswered questions, fleet-wide, from the sessions poll
  // the Board tab's badge off the Board. null = unknown.
  chiefQuestions: null,
  // Owner checks still waiting on the owner (both kinds, Later and Problem
  // left out), from the same poll. null = unknown.
  ownerChecks: null,
  // Session id whose drawer is open. The board poll keeps running;
  // renderBoard() keeps this drawer's node so a reply being typed survives
  // 
  boardExpanded: null,
  // Team OS tab: skills from /api/team-os/skills, plus the
  // read-only content browser's current skill + loaded files.
  teamOsSkills: [],
  // The short git sha /api/version reports, for the Settings header.
  buildSha: null,
  teamOsRecap: null,   // /api/team-os/recap-status, once the tab has read it
  // How many API tokens exist (Settings › API tokens' value); null
  // until Settings first loads the list.
  apiTokenCount: null,
  teamOsBrowser: null,   // { skillId, name, files } while the browser is open
  pendingScan: [],
  webauthn: { configured: false, enrollment_open: false, devices: [] },
  terminal: null,   // { sid, ws, term, fit, onWindowResize }
  // The session overlay's open view: null while closed, else the
  // session it shows and its render mode ('terminal' | 'chat'). Distinct
  // from `terminal` above — a detached session viewed in Chat has no
  // terminal at all, and a full-control session keeps its warm terminal
  // while Chat is showing. Owned by session-overlay.js.
  sessionView: null,
  status: null,     // /api/status payload (incl. terminal reachability)
  // True only when this page was opened as the launcher-spawned PC mirror
  // window — i.e. via the ?terminal=<sid> deep-link (set at boot). A human's own desktop
  // browser over loopback is NOT a mirror, so
  // the connection's loopback reason alone must never flip isMirror — that
  // mis-classification made Stop & Close window.close() the user's Chrome.
  isMirrorWindow: false,
};

// ES modules are deferred — they execute after DOMContentLoaded, so
// document.getElementById is safe to call at module top level.
export const els = {
  homeHeadStatus: document.getElementById('homeHeadStatus'),
  appsHeadStatus: document.getElementById('appsHeadStatus'),
  jobsHeadStatus: document.getElementById('jobsHeadStatus'),
  teamHeadStatus: document.getElementById('teamHeadStatus'),
  boardHeadStatus: document.getElementById('boardHeadStatus'),
  settingsHeadStatus: document.getElementById('settingsHeadStatus'),
  tabApps: document.getElementById('tabApps'),
  tabJobs: document.getElementById('tabJobs'),
  tabTeamOS: document.getElementById('tabTeamOS'),
  teamOsList: document.getElementById('teamOsList'),
  teamOsEmpty: document.getElementById('teamOsEmpty'),
  teamOsEmptyAction: document.getElementById('teamOsEmptyAction'),
  teamOsState: document.getElementById('teamOsState'),
  teamOsFilterInput: document.getElementById('teamOsFilterInput'),
  teamOsFilterEmpty: document.getElementById('teamOsFilterEmpty'),
  teamOsRecap: document.getElementById('teamOsRecap'),
  teamOsRecapList: document.getElementById('teamOsRecapList'),
  teamOsBrowser: document.getElementById('teamOsBrowser'),
  teamOsBrowserTitle: document.getElementById('teamOsBrowserTitle'),
  teamOsBrowserClose: document.getElementById('teamOsBrowserClose'),
  teamOsBrowserDone: document.getElementById('teamOsBrowserDone'),
  teamOsFileList: document.getElementById('teamOsFileList'),
  teamOsDoc: document.getElementById('teamOsDoc'),
  teamOsDocTitle: document.getElementById('teamOsDocTitle'),
  teamOsDocClose: document.getElementById('teamOsDocClose'),
  teamOsDocDone: document.getElementById('teamOsDocDone'),
  teamOsDocLinks: document.getElementById('teamOsDocLinks'),
  teamOsDocDelete: document.getElementById('teamOsDocDelete'),
  teamOsDocRename: document.getElementById('teamOsDocRename'),
  teamOsFileContent: document.getElementById('teamOsFileContent'),
  teamOsDir: document.getElementById('teamOsDir'),
  gitlabGroup: document.getElementById('gitlabGroup'),
  gitlabHost: document.getElementById('gitlabHost'),
  speechEnabledToggle: document.getElementById('speechEnabledToggle'),
  speechBaseUrl: document.getElementById('speechBaseUrl'),
  speechModel: document.getElementById('speechModel'),
  speechApiKey: document.getElementById('speechApiKey'),
  speechApiKeyState: document.getElementById('speechApiKeyState'),
  speechApiKeyClear: document.getElementById('speechApiKeyClear'),
  speechTest: document.getElementById('speechTest'),
  speechTestResult: document.getElementById('speechTestResult'),
  tabBoard: document.getElementById('tabBoard'),

  boardColumns: document.getElementById('boardColumns'),
  boardStatus: document.getElementById('boardStatus'),
  boardFetchState: document.getElementById('boardFetchState'),
  boardRefresh: document.getElementById('boardRefresh'),
  boardFoldAll: document.getElementById('boardFoldAll'),
  boardColBacklog: document.getElementById('boardColBacklog'),
  boardColClaude: document.getElementById('boardColClaude'),
  boardColYours: document.getElementById('boardColYours'),
  boardColOther: document.getElementById('boardColOther'),
  boardColDone: document.getElementById('boardColDone'),
  boardChiefPlan: document.getElementById('boardChiefPlan'),
  boardDispatchRepo: document.getElementById('boardDispatchRepo'),
  boardDispatchRepoInput: document.getElementById('boardDispatchRepoInput'),
  boardDispatchRepoClear: document.getElementById('boardDispatchRepoClear'),
  boardDispatchRepoList: document.getElementById('boardDispatchRepoList'),
  boardDispatchModel: document.getElementById('boardDispatchModel'),
  chiefModelSelect: document.getElementById('chiefModelSelect'),
  chiefModelProblem: document.getElementById('chiefModelProblem'),
  chiefContext: document.getElementById('chiefContext'),
  chiefEffort: document.getElementById('chiefEffort'),
  chiefWorkerCap: document.getElementById('chiefWorkerCap'),
  chiefWorkerCapHelp: document.getElementById('chiefWorkerCapHelp'),
  chiefWorkerCapScopeToggle: document.getElementById('chiefWorkerCapScopeToggle'),
  chiefAnswersDialog: document.getElementById('chiefAnswersDialog'),
  chiefAnswersClose: document.getElementById('chiefAnswersClose'),
  chiefAnswersDefaults: document.getElementById('chiefAnswersDefaults'),
  chiefAnswersList: document.getElementById('chiefAnswersList'),
  chiefAnswersTitleText: document.getElementById('chiefAnswersTitleText'),
  chiefAnswersAlso: document.getElementById('chiefAnswersAlso'),
  boardAnswerAttachTemplate: document.getElementById('boardAnswerAttachTemplate'),
  chiefAnswersNote: document.getElementById('chiefAnswersNote'),
  chiefAnswersDone: document.getElementById('chiefAnswersDone'),

  fleetConfigDir: document.getElementById('fleetConfigDir'),
  skillsDir: document.getElementById('skillsDir'),
  skillsDirStatus: document.getElementById('skillsDirStatus'),

  // Chat pane of the session overlay (transcript, mode)
  chatPane: document.getElementById('chatPane'),
  chatNote: document.getElementById('chatNote'),
  contextRing: document.getElementById('contextRing'),
  contextRingLabel: document.getElementById('contextRingLabel'),
  sessionMode: document.getElementById('sessionMode'),
  sessionModeTerminal: document.getElementById('sessionModeTerminal'),
  sessionModeChat: document.getElementById('sessionModeChat'),
  transcriptBody: document.getElementById('transcriptBody'),
  transcriptOlder: document.getElementById('transcriptOlder'),
  chatLatest: document.getElementById('chatLatest'),
  transcriptList: document.getElementById('transcriptList'),
  transcriptPlanLive: document.getElementById('transcriptPlanLive'),
  transcriptResumeLive: document.getElementById('transcriptResumeLive'),
  transcriptState: document.getElementById('transcriptState'),
  chatComposeBar: document.getElementById('chatComposeBar'),
  chatQueued: document.getElementById('chatQueued'),
  chatQueuedChip: document.getElementById('chatQueuedChip'),
  chatQueuedCount: document.getElementById('chatQueuedCount'),

  // Queued messages sheet (chat/chat-pending.js)
  pendingInputDialog: document.getElementById('pendingInputDialog'),
  pendingInputClose: document.getElementById('pendingInputClose'),
  pendingInputNote: document.getElementById('pendingInputNote'),
  pendingInputList: document.getElementById('pendingInputList'),
  pendingInputEmpty: document.getElementById('pendingInputEmpty'),
  pendingInputDone: document.getElementById('pendingInputDone'),

  // Background tasks sheet
  // Links sheet (links/links-sheet.js)
  linksSheet: document.getElementById('linksSheet'),
  linksClose: document.getElementById('linksClose'),
  linksDone: document.getElementById('linksDone'),
  linksState: document.getElementById('linksState'),
  linksBody: document.getElementById('linksBody'),
  linksGroupTemplate: document.getElementById('linksGroupTemplate'),
  // Show changes overlay
  changesOverlay: document.getElementById('changesOverlay'),
  changesClose: document.getElementById('changesClose'),
  changesRefresh: document.getElementById('changesRefresh'),
  changesTitle: document.getElementById('changesTitle'),
  changesState: document.getElementById('changesState'),
  changesList: document.getElementById('changesList'),

  jobsList: document.getElementById('jobsList'),
  jobsEmpty: document.getElementById('jobsEmpty'),
  jobsLoading: document.getElementById('jobsLoading'),
  jobsFilterEmpty: document.getElementById('jobsFilterEmpty'),
  jobsAddBtn: document.getElementById('jobsAddBtn'),
  jobsFilters: document.getElementById('jobsFilters'),
  jobsSortSheet: document.getElementById('jobsSortSheet'),
  jobsSortTabs: document.getElementById('jobsSortTabs'),
  jobsSortSheetClose: document.getElementById('jobsSortSheetClose'),
  jobsSortSheetClear: document.getElementById('jobsSortSheetClear'),
  jobsSortSheetDone: document.getElementById('jobsSortSheetDone'),
  jobsSearchInput: document.getElementById('jobsSearchInput'),
  jobsNextUpList: document.getElementById('jobsNextUpList'),
  jobsNextUpState: document.getElementById('jobsNextUpState'),
  jobsAgendaOpen: document.getElementById('jobsAgendaOpen'),
  jobsAgendaSheet: document.getElementById('jobsAgendaSheet'),
  jobsAgendaSheetClose: document.getElementById('jobsAgendaSheetClose'),
  jobsAgendaSheetDone: document.getElementById('jobsAgendaSheetDone'),
  jobsAgendaBody: document.getElementById('jobsAgendaBody'),
  jobSheet: document.getElementById('jobSheet'),
  jobSheetTitle: document.getElementById('jobSheetTitle'),
  jobSheetBody: document.getElementById('jobSheetBody'),
  jobSheetClose: document.getElementById('jobSheetClose'),
  jobSheetRun: document.getElementById('jobSheetRun'),
  jobDialog: document.getElementById('jobDialog'),
  jobForm: document.getElementById('jobForm'),
  jobDialogTitle: document.getElementById('jobDialogTitle'),
  jobIdField: document.getElementById('jobIdField'),
  jobNameInput: document.getElementById('jobNameInput'),
  jobKindInput: document.getElementById('jobKindInput'),
  jobScriptRow: document.getElementById('jobScriptRow'),
  jobScriptInput: document.getElementById('jobScriptInput'),
  jobInlineShellFields: document.getElementById('jobInlineShellFields'),
  jobInlineExtInput: document.getElementById('jobInlineExtInput'),
  jobInlineBodyInput: document.getElementById('jobInlineBodyInput'),
  jobHttpCheckFields: document.getElementById('jobHttpCheckFields'),
  jobHttpUrlInput: document.getElementById('jobHttpUrlInput'),
  jobHttpMethodInput: document.getElementById('jobHttpMethodInput'),
  jobHttpExpectStatusInput: document.getElementById('jobHttpExpectStatusInput'),
  jobHttpTimeoutInput: document.getElementById('jobHttpTimeoutInput'),
  jobArgsRow: document.getElementById('jobArgsRow'),
  jobArgsInput: document.getElementById('jobArgsInput'),
  jobScheduleType: document.getElementById('jobScheduleType'),
  jobScheduleEveryRow: document.getElementById('jobScheduleEveryRow'),
  jobScheduleEvery: document.getElementById('jobScheduleEvery'),
  jobScheduleAtRow: document.getElementById('jobScheduleAtRow'),
  jobScheduleAt: document.getElementById('jobScheduleAt'),
  jobScheduleTimesRow: document.getElementById('jobScheduleTimesRow'),
  jobScheduleTimes: document.getElementById('jobScheduleTimes'),
  jobScheduleDayRow: document.getElementById('jobScheduleDayRow'),
  jobScheduleDay: document.getElementById('jobScheduleDay'),
  jobScheduleOnceRow: document.getElementById('jobScheduleOnceRow'),
  jobScheduleOnceAt: document.getElementById('jobScheduleOnceAt'),
  jobCooldownInput: document.getElementById('jobCooldownInput'),
  jobMutexGroupInput: document.getElementById('jobMutexGroupInput'),
  jobConfirmInput: document.getElementById('jobConfirmInput'),
  jobOnSuccessList: document.getElementById('jobOnSuccessList'),
  jobOnFailureList: document.getElementById('jobOnFailureList'),
  jobParamsList: document.getElementById('jobParamsList'),
  jobParamsAdd: document.getElementById('jobParamsAdd'),
  jobWebhookProvider: document.getElementById('jobWebhookProvider'),
  jobWebhookSecretRow: document.getElementById('jobWebhookSecretRow'),
  jobWebhookSecretInput: document.getElementById('jobWebhookSecretInput'),
  jobWebhookEventsRow: document.getElementById('jobWebhookEventsRow'),
  jobWebhookEventsInput: document.getElementById('jobWebhookEventsInput'),
  jobWebhookMappingSection: document.getElementById('jobWebhookMappingSection'),
  jobWebhookMappingList: document.getElementById('jobWebhookMappingList'),
  jobWebhookMappingAdd: document.getElementById('jobWebhookMappingAdd'),
  jobPreflightProblems: document.getElementById('jobPreflightProblems'),
  jobSaveAnyway: document.getElementById('jobSaveAnyway'),
  jobSaveBtn: document.getElementById('jobSaveBtn'),
  jobCancel: document.getElementById('jobCancel'),
  jobRunDialog: document.getElementById('jobRunDialog'),
  jobRunForm: document.getElementById('jobRunForm'),
  jobRunDialogTitle: document.getElementById('jobRunDialogTitle'),
  jobRunDialogStaleNote: document.getElementById('jobRunDialogStaleNote'),
  jobRunDialogFields: document.getElementById('jobRunDialogFields'),
  jobRunDialogDryRun: document.getElementById('jobRunDialogDryRun'),
  jobRunCancel: document.getElementById('jobRunCancel'),

  copilotSkipPerms: document.getElementById('copilotSkipPerms'),
  copilotFlagsPreview: document.getElementById('copilotFlagsPreview'),
  copilotModel: document.getElementById('copilotModel'),
  copilotEffort: document.getElementById('copilotEffort'),
  copilotContext: document.getElementById('copilotContext'),
  copilotAutopilot: document.getElementById('copilotAutopilot'),
  privateLanToggle: document.getElementById('privateLanToggle'),
  screenPrivateLanToggle: document.getElementById('screenPrivateLanToggle'),
  copilotRemote: document.getElementById('copilotRemote'),
  copilotRemoteState: document.getElementById('copilotRemoteState'),
  copilotModels: document.getElementById('copilotModels'),
  copilotMonthlyCredits: document.getElementById('copilotMonthlyCredits'),
  copilotModelsNew: document.getElementById('copilotModelsNew'),
  copilotModelsNewText: document.getElementById('copilotModelsNewText'),
  copilotAddDefaultModels: document.getElementById('copilotAddDefaultModels'),
  claudeList: document.getElementById('claudeList'),
  claudeFilterInput: document.getElementById('claudeFilterInput'),
  claudeFilterEmpty: document.getElementById('claudeFilterEmpty'),
  claudeEmpty: document.getElementById('claudeEmpty'),
  favFilterBtn: document.getElementById('favFilterBtn'),
  imageLightbox: document.getElementById('imageLightbox'),
  imageLightboxImage: document.getElementById('imageLightboxImage'),
  imageLightboxClose: document.getElementById('imageLightboxClose'),
  htmlViewer: document.getElementById('htmlViewer'),
  htmlViewerName: document.getElementById('htmlViewerName'),
  htmlViewerBody: document.getElementById('htmlViewerBody'),
  htmlViewerClose: document.getElementById('htmlViewerClose'),
  htmlViewerDownload: document.getElementById('htmlViewerDownload'),
  gitRefreshBtn: document.getElementById('gitRefreshBtn'),
  gitCheckedFooter: document.getElementById('gitCheckedFooter'),
  projectsSummaryMeta: document.getElementById('projectsSummaryMeta'),
  sessionsList: document.getElementById('sessionsList'),
  sessionsEmpty: document.getElementById('sessionsEmpty'),
  appsList: document.getElementById('appsList'),
  appsFilterInput: document.getElementById('appsFilterInput'),
  appsFilterEmpty: document.getElementById('appsFilterEmpty'),
  appsEmpty: document.getElementById('appsEmpty'),
  appsState: document.getElementById('appsState'),
  traysState: document.getElementById('traysState'),
  registeredTraysList: document.getElementById('registeredTraysList'),
  registeredTraysEmpty: document.getElementById('registeredTraysEmpty'),
  traysSummaryMeta: document.getElementById('traysSummaryMeta'),
  miniAppsCard: document.getElementById('miniAppsCard'),
  miniAppsList: document.getElementById('miniAppsList'),
  miniAppsEmpty: document.getElementById('miniAppsEmpty'),
  miniAppsSummaryMeta: document.getElementById('miniAppsSummaryMeta'),
  appsScanRow: document.getElementById('appsScanRow'),

  tokensList: document.getElementById('tokensList'),
  tokensEmpty: document.getElementById('tokensEmpty'),
  tokenLabelInput: document.getElementById('tokenLabelInput'),
  tokenJobSelect: document.getElementById('tokenJobSelect'),
  tokenMintBtn: document.getElementById('tokenMintBtn'),
  tokenMintResult: document.getElementById('tokenMintResult'),
  tokenMintValue: document.getElementById('tokenMintValue'),
  tokenMintUrl: document.getElementById('tokenMintUrl'),
  tokenCopyBtn: document.getElementById('tokenCopyBtn'),
  documentsSection: document.getElementById('documentsSection'),
  documentsGroup: document.getElementById('documentsGroup'),
  documentsError: document.getElementById('documentsError'),
  documentsRetry: document.getElementById('documentsRetry'),
  documentRowTemplate: document.getElementById('documentRowTemplate'),
  documentSheet: document.getElementById('documentSheet'),
  documentSheetTitle: document.getElementById('documentSheetTitle'),
  documentUpdated: document.getElementById('documentUpdated'),
  documentGithub: document.getElementById('documentGithub'),
  documentCopy: document.getElementById('documentCopy'),
  documentState: document.getElementById('documentState'),
  documentStateMessage: document.getElementById('documentStateMessage'),
  documentRetry: document.getElementById('documentRetry'),
  documentBody: document.getElementById('documentBody'),
  projectsDir: document.getElementById('projectsDir'),
  projectsIgnore: document.getElementById('projectsIgnore'),
  outboxRootsList: document.getElementById('outboxRootsList'),
  outboxRootInput: document.getElementById('outboxRootInput'),
  outboxRootAdd: document.getElementById('outboxRootAdd'),
  appsScanRoot: document.getElementById('appsScanRoot'),
  terminalHistoryLines: document.getElementById('terminalHistoryLines'),
  largeUploadMaxMb: document.getElementById('largeUploadMaxMb'),
  bootAutostartToggle: document.getElementById('bootAutostartToggle'),
  fullscreenToggle: document.getElementById('fullscreenToggle'),
  fullscreenStatus: document.getElementById('fullscreenStatus'),
  installReadinessRow: document.getElementById('installReadinessRow'),
  installReadinessValue: document.getElementById('installReadinessValue'),
  installReadinessStatus: document.getElementById('installReadinessStatus'),
  lidKeepAwakeRow: document.getElementById('lidKeepAwakeRow'),
  lidKeepAwakeToggle: document.getElementById('lidKeepAwakeToggle'),
  lidKeepAwakeHelp: document.getElementById('lidKeepAwakeHelp'),
  lidKeepAwakeStatus: document.getElementById('lidKeepAwakeStatus'),
  diagnosticsWindow: document.getElementById('diagnosticsWindow'),
  diagnosticsSave: document.getElementById('diagnosticsSave'),
  diagnosticsResult: document.getElementById('diagnosticsResult'),
  diagnosticsDownload: document.getElementById('diagnosticsDownload'),
  voiceRemoteHelp: document.getElementById('voiceRemoteHelp'),
  chiefAutoCompactToggle: document.getElementById('chiefAutoCompactToggle'),
  chiefDoorbellToggle: document.getElementById('chiefDoorbellToggle'),
  chiefAutoDrainToggle: document.getElementById('chiefAutoDrainToggle'),
  chiefJobFailuresToggle: document.getElementById('chiefJobFailuresToggle'),
  chiefJobSelfHealToggle: document.getElementById('chiefJobSelfHealToggle'),
  chiefWeeklyProposalsToggle: document.getElementById('chiefWeeklyProposalsToggle'),
  openSessionsInWindowToggle: document.getElementById('openSessionsInWindowToggle'),
  openSessionsInChatToggle: document.getElementById('openSessionsInChatToggle'),
  chiefAutoCompactThreshold: document.getElementById('chiefAutoCompactThreshold'),
  listenersList: document.getElementById('listenersList'),
  listenersEmpty: document.getElementById('listenersEmpty'),
  listenersState: document.getElementById('listenersState'),
  listenersBody: document.getElementById('listenersBody'),
  runningAppsState: document.getElementById('runningAppsState'),
  runningAppsList: document.getElementById('runningAppsList'),
  runningAppsEmpty: document.getElementById('runningAppsEmpty'),
  runningAppsNote: document.getElementById('runningAppsNote'),
  appsIdleLine: document.getElementById('appsIdleLine'),
  appsNowCol: document.getElementById('appsNowCol'),
  otherPortsCardBody: document.getElementById('otherPortsCardBody'),
  otherPortsRow: document.getElementById('otherPortsRow'),
  otherPortsMeta: document.getElementById('otherPortsMeta'),
  otherPortsSheet: document.getElementById('otherPortsSheet'),
  otherPortsSheetClose: document.getElementById('otherPortsSheetClose'),
  listenersCheckAgain: document.getElementById('listenersCheckAgain'),
  statusReadout: document.getElementById('statusReadout'),
  buildReadout: document.getElementById('buildReadout'),

  scanDialog: document.getElementById('scanDialog'),
  scanResults: document.getElementById('scanResults'),
  scanCancel: document.getElementById('scanCancel'),
  scanSave: document.getElementById('scanSave'),

  renameDialog: document.getElementById('renameDialog'),
  renameForm: document.getElementById('renameForm'),
  renameInput: document.getElementById('renameInput'),
  renameCancel: document.getElementById('renameCancel'),

  sessionRenameDialog: document.getElementById('sessionRenameDialog'),
  sessionRenameForm: document.getElementById('sessionRenameForm'),
  sessionRenameInput: document.getElementById('sessionRenameInput'),
  sessionRenameCancel: document.getElementById('sessionRenameCancel'),



  terminalOverlay: document.getElementById('terminalOverlay'),
  terminalBack: document.getElementById('terminalBack'),
  terminalTitle: document.getElementById('terminalTitle'),
  terminalModel: document.getElementById('terminalModel'),
  terminalModelLabel: document.getElementById('terminalModelLabel'),
  modelSwitchSheet: document.getElementById('modelSwitchSheet'),
  modelSwitchSheetClose: document.getElementById('modelSwitchSheetClose'),
  modelSwitchSheetDone: document.getElementById('modelSwitchSheetDone'),
  modelSwitchSheetNote: document.getElementById('modelSwitchSheetNote'),
  modelSwitchList: document.getElementById('modelSwitchList'),
  terminalHost: document.getElementById('terminalHost'),
  terminalStatus: document.getElementById('terminalStatus'),
  terminalActivity: document.getElementById('terminalActivity'),
  terminalLatest: document.getElementById('terminalLatest'),
  terminalComposeBar: document.getElementById('terminalComposeBar'),
  terminalMenu: document.getElementById('terminalMenu'),

  webauthnStatus: document.getElementById('webauthnStatus'),
  webauthnDevices: document.getElementById('webauthnDevices'),
  enrollDeviceBtn: document.getElementById('enrollDeviceBtn'),
};
