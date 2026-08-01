/* Board dispatch bar (issues #302 / #337 / #500 / #547).
 *
 * Split off `board.js` (issue #691, a `/codebase-audit` maintainability
 * finding) the same way `jobs.js` became jobs-row/jobs-dialog/jobs-agenda and
 * `terminal.js` became eight feature-split modules. `board.js` keeps card
 * rendering, the drill-down drawer, one-tap issue-start and the column
 * carousel; everything the *bar above the columns* owns lives here:
 *
 * - the repo/project combo (#337), which doubles as the kanban's card filter
 *   (`boardRepoFilter` / `matchesRepoFilter`, read by `renderBoard`);
 * - free-text dispatch (#302) — goal → a fresh /issue-add | /issue-add now |
 *   /issue-yolo session via POST /api/board/dispatch (spawn-then-type
 *   server-side, so free text never touches a spawn command line).
 *
 * The bar is static markup `renderBoard()` never touches, so the 5 s poll
 * can't wipe a goal being typed.
 *
 * `board.js` and this module import each other (this one calls `renderBoard`
 * and `fetchBoard`; `board.js` calls `wireDispatch`/`syncDispatchBar` and the
 * two filter helpers). That mirrors the existing, working sessions.js <->
 * terminal.js cycle — every cross-module call happens inside a function body,
 * never at module-evaluation time.
 */

import { els, state } from './state.js';
import { apiFailToast, authHeaders, jsonApi, toast } from './api.js';
import { applyLaunchSizePayload } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { ensureTerminalToken } from './webauthn.js';
import { fetchBoard, renderBoard } from './board.js';

// Tiny "working" indicator: swap a button's label for a ticking
// elapsed-seconds timer so a blind background wait visibly shows progress
// instead of looking stuck. ``workingLabel`` defaults to the hourglass
// glyph; pass a richer label for wide buttons. Labels are HTML (the Lucide
// icon() markup rides them — issue #355 PR 3). Returns a stop() that
// restores ``restoreHtml``.
function startWorkTimer(btn, restoreHtml, workingLabel) {
  const lbl = workingLabel || icon('hourglass') + ' ';
  const t0 = Date.now();
  btn.classList.add('working');
  function tick() {
    const s = Math.floor((Date.now() - t0) / 1000);
    btn.innerHTML = lbl + s + 's';
  }
  tick();
  const id = setInterval(tick, 500);
  return function stop() {
    clearInterval(id);
    btn.classList.remove('working');
    btn.innerHTML = restoreHtml;
  };
}

// -------------------------------------------------------- dispatch (#302)

let dispatchMode = 'add';

// Repo/project dropdown (#337) ← the same live coding listing the
// Coding tab renders (state.apps). It is a plain tap-to-open/tap-to-select
// dropdown, not a typable field — a button trigger, not an <input>. It does
// double duty: the real dispatch target lives in the hidden
// #boardDispatchRepo input dispatchGoal() reads, AND the current selection
// (or "All projects", the default) filters which cards renderBoard() shows
// in every column via boardRepoFilter()/cardRepoOf() below.
// Re-synced on tab activation and on every board render (so a boot /api/apps
// fetch that lands late still populates it), but the underlying name list is
// only rebuilt when it actually changed — a rebuild mid-browse would
// otherwise reset a dropdown the user has open.
let _repoSig = null;
let _repoNames = [];

const ALL_PROJECTS_LABEL = 'All projects';

function repoListOpen() {
  const list = els.boardDispatchRepoList;
  return !!list && !list.hidden;
}

function repoDisplayLabel(name) {
  return name || ALL_PROJECTS_LABEL;
}

function renderRepoList() {
  const list = els.boardDispatchRepoList;
  const hidden = els.boardDispatchRepo;
  if (!list) return;
  list.replaceChildren();
  const allLi = document.createElement('li');
  allLi.textContent = ALL_PROJECTS_LABEL;
  allLi.dataset.repo = '';
  allLi.setAttribute('role', 'option');
  allLi.setAttribute('aria-selected', hidden.value === '' ? 'true' : 'false');
  list.appendChild(allLi);
  _repoNames.forEach(function (name) {
    const li = document.createElement('li');
    li.textContent = name;
    li.dataset.repo = name;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', name === hidden.value ? 'true' : 'false');
    list.appendChild(li);
  });
}

function openRepoList() {
  const list = els.boardDispatchRepoList;
  const btn = els.boardDispatchRepoBtn;
  if (!list || !btn) return;
  renderRepoList();
  list.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
}

function closeRepoList() {
  const list = els.boardDispatchRepoList;
  const btn = els.boardDispatchRepoBtn;
  if (!list || !btn) return;
  list.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
}

function selectRepo(name) {
  els.boardDispatchRepo.value = name;
  els.boardDispatchRepoBtn.textContent = repoDisplayLabel(name);
  closeRepoList();
  // The same selection scopes the visible kanban cards (#337) — apply it
  // immediately rather than waiting for the next 5 s poll.
  renderBoard();
}

function syncDispatchRepos() {
  const hidden = els.boardDispatchRepo;
  const btn = els.boardDispatchRepoBtn;
  if (!hidden || !btn) return;
  const repos = (state.apps || [])
    .filter(function (a) { return a.kind === 'coding'; })
    .map(function (a) { return String(a.name); });
  const sig = repos.join('\n');
  if (sig === _repoSig) return;
  _repoSig = sig;
  _repoNames = repos;
  // '' ("All projects") is always a valid selection — only reset a specific
  // repo pick back to "All" if that repo dropped out of the live list.
  const current = hidden.value;
  const next = (!current || repos.indexOf(current) >= 0) ? current : '';
  hidden.value = next;
  btn.textContent = repoDisplayLabel(next);
  if (repoListOpen()) renderRepoList();
}

// The repo/project identity of a card, whatever kind it is — issue/PR/done
// cards carry `repo`, live session cards carry `project`. Job cards carry
// neither (they aren't tied to a coding project) and are hidden by a
// specific-project filter, same as any other non-matching card.
function cardRepoOf(card) {
  return card.repo || card.project || null;
}

export function boardRepoFilter() {
  return els.boardDispatchRepo ? els.boardDispatchRepo.value : '';
}

export function matchesRepoFilter(card, filter) {
  if (!filter) return true;
  const repo = cardRepoOf(card);
  return !!repo && String(repo).toLowerCase() === String(filter).toLowerCase();
}

function setDispatchMode(mode) {
  dispatchMode = mode;
  // The select itself already shows the chosen value (#547 collapsed the
  // old 4-segment radiogroup into a single <select>) — nothing else to
  // paint here.
}

async function dispatchGoal() {
  const goal = els.boardDispatchGoal.value.trim();
  if (!goal) {
    toast('Type or dictate a goal first', 'error');
    return;
  }
  const repo = els.boardDispatchRepo.value;
  if (!repo) {
    toast('No repo to dispatch to', 'error');
    return;
  }
  const btn = els.boardDispatchSend;
  btn.disabled = true;
  // The server waits for the agent's first output before typing the goal
  // in (spawn-then-type), so this call legitimately takes seconds — tick.
  const stopTimer = startWorkTimer(btn, icon('send-horizontal'));
  try {
    const tt = await ensureTerminalToken();
    const payload = {
      repo: repo,
      goal: goal,
      mode: dispatchMode,
      // '' = the Copilot auto model (no --model on the launch line).
      model: (els.boardDispatchModel && els.boardDispatchModel.value) || '',
    };
    // Same size contract as startIssue (issue #374).
    applyLaunchSizePayload(payload);
    const body = await jsonApi('/api/board/dispatch', {
      method: 'POST',
      headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
      body: JSON.stringify(payload),
    });
    toast((body.launched || dispatchMode) + ' → ' + (body.repo || repo), 'good', { icon: 'rocket' });
    // The goal stays in the bar for rapid multi-dispatch ("create more");
    // ✕ clears it. The new card lands in Bot's turn on the next poll.
    fetchBoard().catch(function () {});
  } catch (exc) {
    apiFailToast('Dispatch failed', exc);
  } finally {
    stopTimer();
    btn.disabled = false;
  }
}

export function syncDispatchBar() {
  syncDispatchRepos();
  syncDispatchModels();
}

// Model <select> options ← the config-driven copilot_models list (#500,
// reduced to Copilot-only in the lite fork). Rebuilt only when the list
// actually changes so a rebuild can't reset a dropdown mid-browse; the
// "Default" option ('' — Copilot picks the model) is always first.
let _modelSig = null;

function syncDispatchModels() {
  const sel = els.boardDispatchModel;
  if (!sel) return;
  const cp = (state.config && state.config.copilot) || {};
  const models = (cp.models_available || []).map(String);
  const sig = models.join('\n');
  if (sig === _modelSig) return;
  _modelSig = sig;
  const current = sel.value;
  sel.replaceChildren();
  const defOpt = document.createElement('option');
  defOpt.value = '';
  defOpt.textContent = 'Default';
  sel.appendChild(defOpt);
  models.forEach(function (m) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  });
  sel.value = models.indexOf(current) >= 0 ? current : '';
}

function wireRepoCombo() {
  const btn = els.boardDispatchRepoBtn;
  const list = els.boardDispatchRepoList;
  const combo = btn && btn.closest('.board-repo-combo');
  if (!btn || !list || !combo) return;
  btn.addEventListener('click', function () {
    if (repoListOpen()) closeRepoList(); else openRepoList();
  });
  btn.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeRepoList();
  });
  list.addEventListener('click', function (e) {
    const li = e.target.closest('li[data-repo]');
    if (!li) return;
    selectRepo(li.dataset.repo);
  });
  // Tapping anywhere outside the trigger/list closes it — there's no text
  // focus to lose (it's a button, not an input), so a plain outside-click
  // check is enough; no blur-race handling needed.
  document.addEventListener('click', function (e) {
    if (repoListOpen() && !combo.contains(e.target)) closeRepoList();
  });
}

export function wireDispatch() {
  if (!els.boardDispatchSend) return;
  wireRepoCombo();
  if (els.boardDispatchMode) {
    els.boardDispatchMode.addEventListener('change', function () {
      setDispatchMode(els.boardDispatchMode.value);
    });
  }
  // The model <select> (#500) is a plain client-side control (issue #355
  // pattern) — no server config, just read at dispatch time above.
  els.boardDispatchSend.addEventListener('click', function () {
    dispatchGoal();
  });
  els.boardDispatchClear.addEventListener('click', function () {
    els.boardDispatchGoal.value = '';
    els.boardDispatchGoal.focus();
  });
  syncDispatchBar();
}
