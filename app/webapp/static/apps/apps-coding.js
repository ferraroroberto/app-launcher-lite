/* Coding tab: project tiles, the Copilot launch button, favorites, the
 * launch toolbar, and the git-status annotation (row meta, the card's
 * uncommitted count and "checked" footer).
 *
 * Split out of apps.js, following the same shape jobs.js →
 * jobs-row/jobs-dialog/jobs-agenda and board.js → board-dispatch already
 * set. The import cycle back to apps.js is deliberate and matches those
 * siblings: apps.js owns the shared list orchestration (renderApps,
 * launchApp, fetchApps) that both tab surfaces drive.
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, logPollFailure, toast } from '../api.js';
import { agoFromMinutes } from '../shared/format.js';
import { chip } from '../glance.js';
import { mountLaunchToolbar } from '../launch-toolbar.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';
import { ACTION_ROW_MENU, actionRow } from '../action-rows.js';
import { listFilter } from '../list-filter.js';
import { openChanges } from '../lazy-views.js';
import { icon } from '../_vendored/icons/icons.js';
import { fetchApps, launchApp, renderApps } from '../apps.js';
import { openSettingsAt } from '../tabs.js';
import { canPull, fetchAll, pullProject, syncAll, wireGitSync } from './git-sync.js';

const ISSUES_LABEL = 'GitLab issues';
const VSCODE_BUTTON_LABEL = 'Visual Studio Code';

// The Projects card's launch toolbar: a launch preset, Detached and Resume. Mounted at load so
// launchApp (apps.js) reads the switches through it. The ids are the hooks
// the launch and e2e code use.
export const codingLaunch = mountLaunchToolbar(document.getElementById('codingLaunchToolbar'), {
  ids: { detached: 'claudeDetached', resume: 'claudeResume' },
  presetKey: 'coding',
  detachedTitle: 'Launch detached: a console window on the PC, listed and killable here but no phone terminal',
  resumeTitle: 'Resume: reopen the agent\u2019s own session picker, in a phone terminal, or in the detached console when Detached is also on',
  // Sync all and the git refresh ride the switch row, pinned right.
  trailing: document.getElementById('gitToolbarActions'),
});

// The ⋯ menu shared by every Coding row; drops below the row and survives
// the ~4 s apps re-render.
const projectMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });
// The Projects list's name filter, re-applied after every render.
const projectsFilter = listFilter({
  input: els.claudeFilterInput,
  list: els.claudeList,
  empty: els.claudeFilterEmpty,
  storageKey: 'app-launcher.filter.projects',
});

// ------------------------------------------------------ Coding tab tiles
// A Coding tile shows the bare on-disk folder name plus exactly three
// controls: the Copilot launch (the row itself), the ⋯ project menu, and the
// favorite star. The launch is disabled with a hover hint when the Copilot
// CLI isn't installed. Coding rows are disk-scanned, so they carry no
// rename/remove controls.
export function renderCodingList(host, items) {
  host.innerHTML = '';
  // Favorites pinned to the top. `items` arrives alphabetical
  // from the scanner, so a stable partition keeps both groups A–Z. The
  // "Favorites" header toggle (state.codingFavFilter) narrows the list to
  // just the starred ones.
  const favs = items.filter(function (a) { return a.is_favorite; });
  const rest = items.filter(function (a) { return !a.is_favorite; });
  const ordered = state.codingFavFilter ? favs : favs.concat(rest);
  syncFavFilterBtn();
  renderGitSummary(items);

  if (state.codingFavFilter && favs.length === 0) {
    const note = document.createElement('li');
    note.className = 'coding-fav-empty muted small';
    note.innerHTML = 'No favorites yet — tap a project’s ' + icon('star') + ' to star it.';
    host.appendChild(note);
    projectMenu.close();
    projectsFilter.apply();
    return;
  }

  // Copilot is the one agent; the registry says whether its CLI is installed.
  const agent = (state.agents || []).find(function (x) { return x.id === 'copilot'; });

  ordered.forEach(function (a) {
    const gs = state.gitStatus && state.gitStatus[a.id];
    const git = gitFlags(gs);
    // The row itself launches Copilot (the action-row contract: tapping the
    // row is its primary action).
    const row = actionRow({
      id: a.id,
      className: 'coding-item',
      title: a.name,
      meta: git.meta,
      chips: git.chips,
      label: agent ? 'Launch ' + agent.label + ' in ' + a.name : a.name,
      disabled: !agent || !agent.available,
      hint: agent ? agent.label + ' is not installed' : 'No launch agent',
      onMain: function () { launchApp(a, agent.id); },
      favorite: { on: a.is_favorite, onToggle: function () { toggleFavorite(a); } },
      kebabClass: 'project-menu-anchor',
      kebabLabel: 'Project actions',
    });
    if (agent) row.main.dataset.agent = agent.id;
    // The row's git state as a hook (no colour: the meta line
    // and the chip say it).
    if (git.flag) row.li.dataset.git = git.flag;

    const menuEl = projectMenu.attach(a.id, row.kebab, projectMenuItems(a, gs));
    if (menuEl) menuEl.dataset.surface = 'code.project-menu';
    host.appendChild(row.li);
  });
  // An open menu whose row is gone drops its state; a reopened one keeps it.
  projectMenu.endRender();
  projectsFilter.apply();
}

// The project row's ⋯ menu items (its kebab), in menu order.
function projectMenuItems(a, gs) {
  // ⋯ project menu — everything a project row offers besides the launch and
  // the star:
  //   · GitLab issues — the repo's issues list, pre-filtered by the server
  //     (`issues_url`), in a new tab. Greyed with a hint when the project has
  //     no known remote.
  //   · Open in VS Code — the project's sibling `.code-workspace`,
  //     created server-side first if missing. Greyed when the `code` CLI
  //     isn't on PATH.
  //   · Show changes — the read-only working-tree viewer (changes-
  //     overlay.js). Hidden once git-status says the folder isn't a repo.
  //   · Pull — a fast-forward, only when the server would do one.
  //   · Open folder — the project directory in Explorer on the PC.
  // Declared in the item list rather than appended conditionally:
  // the row menu detaches a hidden row instead of marking it [hidden], so a
  // count of the menu's buttons only ever sees what is actually offered.
  const menuItems = [{
    dataset: { item: 'project-github-btn' },
    brand: 'b-gitlab',
    label: ISSUES_LABEL,
    ariaLabel: a.issues_url ? 'Open GitLab issues' : 'No GitLab remote',
    disabled: !a.issues_url,
    hint: 'No GitLab remote',
    onTap: function () {
      window.open(a.issues_url, '_blank', 'noopener,noreferrer');
    },
  }];
  return menuItems.concat([
    {
      dataset: { item: 'project-vscode-btn' },
      brand: 'b-vscode',
      label: 'Open in VS Code', ariaLabel: 'Open in ' + VSCODE_BUTTON_LABEL,
      disabled: !state.vscodeAvailable,
      hint: VSCODE_BUTTON_LABEL + ' is not installed',
      onTap: function () { openInVscode(a); },
    },
    {
      dataset: { item: 'project-changes-btn' }, glyph: 'git-branch',
      label: 'Show changes',
      hidden: !!(gs && !gs.is_git),
      onTap: function () { openChanges(a); },
    },
    {
      // Pull: only for a repo the server would fast-forward (clean, on its
      // default branch, only behind), the same rule Sync all applies.
      dataset: { item: 'project-pull-btn' }, glyph: 'download',
      label: 'Pull',
      hidden: !canPull(gs),
      onTap: function () { pullProject(a).then(function () { refreshGitStatus({ quiet: true, fresh: true }); }); },
    },
    {
      dataset: { item: 'project-folder-btn' }, glyph: 'folder',
      label: 'Open folder',
      onTap: function () { openFolder(a); },
    },
  ]);
}

// Open the project directory in Explorer on the PC. Fire-and-report,
// like VS Code below: Explorer is its own app, nothing to track afterwards.
async function openFolder(a) {
  try {
    await jsonApi('/api/claude-code/folder/' + encodeURIComponent(a.id), { method: 'POST' });
    toast('Opening ' + a.name + ' in Explorer', 'ok');
  } catch (exc) {
    apiFailToast('Could not open folder', exc);
  }
}

// Open a coding project in the local VS Code. Fire-and-report:
// the server creates the `.code-workspace` file if needed and spawns `code`,
// which detaches into its own GUI — so there's no session to poll, no list to
// re-fetch, and the only feedback worth showing is whether the spawn landed.
async function openInVscode(a) {
  try {
    const body = await jsonApi('/api/claude-code/vscode/' + encodeURIComponent(a.id), {
      method: 'POST',
    });
    // The workspace file being *created* is worth naming — it's a new file on
    // disk the user didn't ask for by name; a plain open isn't.
    toast(
      body && body.created
        ? 'Created ' + a.name + '.code-workspace — opening VS Code'
        : 'Opening ' + a.name + ' in VS Code',
      'ok'
    );
  } catch (exc) {
    apiFailToast('Could not open VS Code', exc);
  }
}

// Star / unstar a coding project. Persists server-side, then
// re-fetches /api/apps so the star and the favorites-first ordering update
// from the authoritative payload (no optimistic local mutation to drift).
async function toggleFavorite(a) {
  try {
    await jsonApi('/api/claude-code/favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: a.id, favorite: !a.is_favorite }),
    });
    await fetchApps();
  } catch (exc) {
    apiFailToast('Could not update favorite', exc);
  }
}

// Keep the "Favorites" header toggle's pressed state + glyph in sync with
// state.codingFavFilter. Called on every coding re-render so the 4 s apps
// poll can't leave the button out of step with the list it's filtering.
function syncFavFilterBtn() {
  const btn = els.favFilterBtn;
  if (!btn) return;
  const on = state.codingFavFilter;
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  // Icon only, the filter field's trailing star. The
  // name lives on aria-label ("Filter favorites") and title, where a screen
  // reader reads it anyway.
  btn.innerHTML = icon('star');
}

// What git-status says about a project row (quiet by design): the row's context line names the
// branch when it is parked off
// its default one, in the plain meta colour, and uncommitted work is an
// attention chip (amber, never red: it needs you soon, it is not broken).
// Nothing for a folder that isn't a repo or hasn't
// been scanned yet. `flag` is a hook, not a colour.
function gitFlags(gs) {
  if (!gs || !gs.is_git) return { flag: '', meta: '', chips: [] };
  const offMain = !!gs.branch && !gs.on_default_branch;
  const chips = gs.dirty ? [chip('uncommitted', 'attention', 'git-uncommitted')] : [];
  const upChip = upstreamChip(gs.upstream);
  if (upChip) chips.push(upChip);
  // Sync all moved this checkout under an app that still runs the old code.
  if (gs.restart_needed) chips.push(chip('restart needed', 'attention', 'git-restart-needed'));
  return {
    flag: gs.dirty ? 'dirty' : (offMain ? 'off-main' : ''),
    meta: offMain ? 'on ' + gs.branch : '',
    chips: chips,
  };
}

// Where the default branch stands against origin, apart from the dirty
// chip. Up to date, no remote and not yet fetched say nothing; a failed
// fetch says unknown, never up to date.
function upstreamChip(up) {
  if (!up) return null;
  if (up.state === 'behind') return chip('behind ' + up.behind, 'attention', 'git-behind');
  if (up.state === 'diverged') return chip('diverged', 'attention', 'git-diverged');
  if (up.state === 'ahead') return chip('ahead ' + up.ahead, 'neutral', 'git-ahead');
  if (up.state === 'unknown') return chip('remote unknown', 'neutral', 'git-upstream-unknown');
  return null;
}

// The Projects card's git lines: the uncommitted count in its
// summary, with an attention dot, and when git was last checked as its
// footer. Both stay hidden until the first git-status fetch lands.
function renderGitSummary(items) {
  const meta = els.projectsSummaryMeta;
  const footer = els.gitCheckedFooter;
  const known = !!state.gitStatus;
  if (meta) {
    const dirty = known ? items.filter(function (a) {
      const gs = state.gitStatus[a.id];
      return gs && gs.is_git && gs.dirty;
    }).length : 0;
    meta.hidden = !dirty;
    if (dirty) {
      meta.innerHTML = '<span class="tone-dot" data-tone="attention" aria-hidden="true"></span>';
      meta.appendChild(document.createTextNode(dirty + ' uncommitted'));
    }
  }
  if (footer) {
    footer.hidden = !(known && state.gitStatusAt);
    if (!footer.hidden) footer.textContent = 'Git checked ' + checkedAgo(state.gitStatusAt);
  }
}

function checkedAgo(ms) {
  return agoFromMinutes(Math.floor((Date.now() - ms) / 60000));
}

// Always-on git-status refresh (deliberately not on-demand).
// Runs git per project on the server, fanned out
// across threads; caches the result in state and re-renders every surface
// that reads it (Coding rows, the Projects summary and footer; the Board's
// Backlog stopped drawing git state). Called at boot and on the
// GIT_STATUS_POLL_MS interval in main.js (quiet — poll failures log, never
// toast), and by the Projects toolbar's refresh button below (loud). The server
// answers from memory with the answer's age; only the button asks for a scan
// that starts now (`fresh`).
export async function refreshGitStatus(options) {
  const quiet = !!(options && options.quiet);
  const fresh = !!(options && options.fresh);
  try {
    const body = await jsonApi('/api/claude-code/git-status' + (fresh ? '?fresh=true' : ''));
    const map = {};
    (body.projects || []).forEach(function (p) { map[p.id] = p; });
    state.gitStatus = map;
    state.gitStatusAt = Date.now() - (Number(body.age_seconds) || 0) * 1000;
    renderApps();
  } catch (exc) {
    if (!quiet) throw exc;
    logPollFailure('git status refresh failed', exc);
  }
}

// The Projects toolbar's refresh: fetch every remote, then re-run git now.
// The footer's "Git checked just now" is the confirmation. A fetch that
// fails still re-reads status, so its repos show as unknown.
async function checkGitNow() {
  const btn = els.gitRefreshBtn;
  if (btn) btn.disabled = true;
  try {
    try {
      await fetchAll();
    } catch (exc) {
      apiFailToast('Fetch failed', exc);
    }
    await refreshGitStatus({ fresh: true });
  } catch (exc) {
    apiFailToast('Git status check failed', exc);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// The Projects card's controls, wired from apps.js::wireApps so this
// module owns every listener that reads its own state.
export function wireCoding() {
  // No projects found: its fix is the Projects folder.
  const claudeEmptyAction = document.getElementById('claudeEmptyAction');
  if (claudeEmptyAction) {
    claudeEmptyAction.addEventListener('click', function () {
      openSettingsAt('projectsDir');
    });
  }
  if (els.gitRefreshBtn) els.gitRefreshBtn.addEventListener('click', checkGitNow);
  const syncBtn = document.getElementById('gitSyncAllBtn');
  if (syncBtn) {
    syncBtn.addEventListener('click', function () {
      syncBtn.disabled = true;
      syncAll(function () {
        syncBtn.disabled = false;
        refreshGitStatus({ quiet: true, fresh: true });
      });
    });
  }
  wireGitSync();
  if (els.favFilterBtn) {
    els.favFilterBtn.addEventListener('click', function (event) {
      // The trailing star inside the filter field. The field is a
      // <label>, so keep the tap from also focusing the input.
      event.preventDefault();
      state.codingFavFilter = !state.codingFavFilter;
      localStorage.setItem(
        'launcher.codingFavFilter', state.codingFavFilter ? '1' : '0'
      );
      renderApps();
    });
  }
}
