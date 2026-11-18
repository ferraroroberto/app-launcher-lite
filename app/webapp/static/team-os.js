/* Team OS tab: skill rows + one-tap launch, the weekly recap glance row
 * and the read-only content browser (team-os/team-os-content.js).
 *
 * The Life tab's shape in the full app, cut to what team-os offers. A row's
 * tap launches a GitHub Copilot CLI session in the team-os repo that
 * auto-invokes the bare /<skill> slash-command; the model, Detached and
 * Resume controls are the shared launch toolbar Code › Projects mounts
 * (launch-toolbar.js), at the top of the Skills card. The model combo lists
 * Default ("" — Copilot picks) plus the config's Copilot models.
 */

import { els, state } from './state.js';
import { renderHomeHead } from './home-head.js';
import { logPollFailure, jsonApi } from './api.js';
import { applyLaunchSizePayload } from './shared/launch-size.js';
import { handleLaunchResponse } from './terminal.js';
import { SensitiveOutcome, runSensitiveAction } from './shared/sensitive-action.js';
import { icon } from './_vendored/icons/icons.js';
import { wireModelCombo } from './shared/model-combo.js';
import { ACTION_ROW_MENU, actionRow } from './action-rows.js';
import { avatar, chip } from './glance.js';
import { failedFetchState, setAsyncState } from './async-state.js';
import { mountLaunchToolbar } from './launch-toolbar.js';
import { listFilter } from './list-filter.js';
import { openSettingsAt } from './tabs.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';
import { terminalJsonApi } from './webauthn.js';
import { openBrowser, wireContentBrowser } from './team-os/team-os-content.js';

// The skill rows' kebab menu, on the vendored row-menu.
const skillMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });

// The Skills card's launch switches: the shared toolbar Code › Projects
// mounts. The model combo beside it is the pane's own markup.
const teamLaunch = mountLaunchToolbar(document.getElementById('teamOsLaunchToolbar'), {
  ids: { detached: 'teamOsDetached', resume: 'teamOsResume' },
  detachedTitle: 'Launch detached: a console window on the PC, listed and killable in the Coding tab but no phone terminal',
  resumeTitle: 'Resume: reopen Copilot’s session picker (drops the skill prompt), in a phone terminal, or in the detached console when Detached is also on',
});

function launchDetached() { return !!(teamLaunch && teamLaunch.isDetached()); }
function launchResume() { return !!(teamLaunch && teamLaunch.isResume()); }

// The Skills list's name filter (the Apps filter's twin), re-applied
// after every render.
const skillsFilter = listFilter({
  input: els.teamOsFilterInput,
  list: els.teamOsList,
  empty: els.teamOsFilterEmpty,
  storageKey: 'app-launcher.filter.team-skills',
});

// The launch-model dropdown controller ({setValue, getValue, setOptions}).
// Read at launch time; no server round-trip — it's per-launch.
let teamOsModelCombo = null;

// '' is Default: let Copilot pick its own model.
function teamOsModel() {
  return (teamOsModelCombo && teamOsModelCombo.getValue()) || '';
}

// Default plus the config's Copilot models, rebuilt whenever the config
// lands (settings/claude-options.js calls it on every config read).
export function setTeamOsModelOptions() {
  if (!teamOsModelCombo) return;
  const models = ((state.config || {}).copilot || {}).models_available || [];
  const current = teamOsModel();
  teamOsModelCombo.setOptions([{ value: '', label: 'Default' }].concat(
    models.map(function (m) { return { value: m, label: m }; })
  ));
  teamOsModelCombo.setValue(models.indexOf(current) !== -1 ? current : '');
}

// ----------------------------------------------------------- skills list
// When the skills last read well: a failure before it is the
// list's error with Retry, one after it a stale line over the last list.
let skillsReadAt = 0;

function retrySkills() {
  fetchSkills().catch(function () {});
}

export async function fetchSkills() {
  if (!skillsReadAt) setAsyncState(els.teamOsState, 'loading', { message: 'Reading skills…' });
  try {
    const body = await jsonApi('/api/team-os/skills');
    skillsReadAt = Date.now();
    setAsyncState(els.teamOsState, 'ready');
    state.teamOsSkills = body.skills || [];
    renderSkills();
  } catch (exc) {
    logPollFailure('team-os skills fetch failed', exc);
    failedFetchState(els.teamOsState, !!skillsReadAt, skillsReadAt, 'the skills', retrySkills);
  }
}

// The avatar's glyph: the name's first letter.
function skillAvatar(s) {
  const glyph = document.createElement('span');
  glyph.className = 'teamos-monogram';
  glyph.setAttribute('aria-hidden', 'true');
  glyph.textContent = String(s.name || s.id || '?').charAt(0).toUpperCase();
  return avatar(glyph, 'none', 'teamos-avatar');
}

function skillMenuItems(s) {
  return [
    {
      dataset: { item: 'teamos-start-new-btn' }, glyph: 'circle-plus',
      label: 'Start new session',
      ariaLabel: 'Start a new session with ' + s.name,
      onTap: function () { launchSkill(s, { picker: false }); },
    },
    {
      dataset: { item: 'teamos-pick-session-btn' }, glyph: 'clipboard-list',
      label: 'Pick a session…',
      ariaLabel: 'Open Copilot’s session picker for ' + s.name,
      onTap: function () { launchSkill(s, { picker: true }); },
    },
    {
      dataset: { item: 'teamos-browse-btn' }, glyph: 'book-open',
      label: 'Read', ariaLabel: 'Browse ' + s.name,
      onTap: function () { openBrowser(s); },
    },
  ];
}

export function renderSkills() {
  const host = els.teamOsList;
  if (!host) return;
  renderHomeHead();
  host.innerHTML = '';
  const skills = state.teamOsSkills.slice().sort(function (a, b) {
    return String(a.name).localeCompare(String(b.name));
  });
  // "No skills found" only after a good read: before one, an empty list is
  // teamOsState's loading or error, not an empty folder.
  els.teamOsEmpty.hidden = skills.length !== 0 || !skillsReadAt;
  skills.forEach(function (s) {
    const row = actionRow({
      id: s.id,
      className: 'teamos-item',
      title: s.name,
      meta: s.description || '',
      avatar: skillAvatar(s),
      label: 'Launch ' + s.name,
      onMain: function () { launchSkill(s); },
      kebabClass: 'teamos-menu-anchor',
      kebabLabel: 'Skill actions',
    });
    const menuEl = skillMenu.attach(s.id, row.kebab, skillMenuItems(s));
    if (menuEl) menuEl.dataset.surface = 'team.skill-menu';
    host.appendChild(row.li);
  });
  // An open menu whose row is gone drops its state; a reopened one keeps it.
  skillMenu.endRender();
  skillsFilter.apply();
}

// ------------------------------------------------- weekly recap
// One glance row under the skills: how long since the recap ledger
// last moved, a chip only for an exception (due or overdue, a draft ready),
// and ▶, which launches /weekly-recap (the interactive review) with the
// Skills toolbar's model and Detached switch.
export async function fetchRecapStatus() {
  try {
    state.teamOsRecap = await jsonApi('/api/team-os/recap-status');
    renderRecap();
  } catch (exc) {
    logPollFailure('team-os recap-status fetch failed', exc);
  }
}

// "last run 3 days ago", "last run today" or "never run".
function recapMeta(r) {
  if ((r.staleness || 'never') === 'never') return 'never run';
  const d = Math.round(r.age_days || 0);
  if (d <= 0) return 'last run today';
  return 'last run ' + d + (d === 1 ? ' day ago' : ' days ago');
}

function renderRecap() {
  const host = els.teamOsRecap;
  const list = els.teamOsRecapList;
  if (!host || !list) return;
  const r = state.teamOsRecap;
  // The header carries the recap's exception (home-head.js).
  renderHomeHead();
  if (!r || !r.available) {
    host.hidden = true;
    list.replaceChildren();
    return;
  }
  host.hidden = false;
  const status = r.staleness || 'never';
  const chips = [];
  if (status === 'due' || status === 'overdue') {
    chips.push(chip(status, 'attention', 'teamos-recap-chip'));
  }
  if (r.proposal_pending) chips.push(chip('draft ready', 'accent', 'teamos-recap-chip'));
  const row = actionRow({
    id: 'weekly-recap',
    className: 'teamos-recap-item',
    title: 'Weekly recap',
    tail: recapMeta(r),
    chips: chips,
    label: 'Review the weekly recap',
    onMain: launchRecap,
    avatar: avatar('notebook-text', 'none', 'teamos-recap-avatar'),
  });
  const verb = document.createElement('button');
  verb.dataset.track = 'team-os.recap-launch';
  verb.type = 'button';
  verb.id = 'teamOsRecapLaunch';
  verb.className = 'icon-button action-row-verb teamos-recap-launch';
  verb.innerHTML = icon('play');
  verb.title = 'Review the weekly recap (/weekly-recap)';
  verb.setAttribute('aria-label', 'Run weekly recap review');
  verb.addEventListener('click', launchRecap);
  row.li.appendChild(verb);
  list.replaceChildren(row.li);
}

// Toast suffix for the launch model: silent on Default, " (<id>)" otherwise.
function modelTag(model) {
  return model ? ' (' + model + ')' : '';
}

// One launch, the recap's or a skill's: passkey-gated like every spawn, so
// the terminal token rides along. A full-control session drops straight
// into the terminal; a detached one only appears in the Coding tab's list.
async function launch(spec) {
  const mode = launchDetached() ? 'remote' : 'pty';
  const payload = Object.assign({ mode: mode, model: teamOsModel() }, spec.body);
  // The phone carries its real terminal size so the PTY spawns at the
  // width the overlay will fit() to; a remote launch has no terminal.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  const verdict = await runSensitiveAction({
    key: spec.key,
    pending: spec.pending,
    icon: spec.icon,
    run: function () { return terminalJsonApi(spec.path, { method: 'POST', body: payload }); },
    done: function () {
      return spec.done + (spec.picker ? '' : modelTag(payload.model)) + (mode === 'remote' ? ' (detached)' : '');
    },
    failPrefix: spec.failPrefix,
    unconfirmed: spec.unconfirmed,
  });
  if (verdict.outcome === SensitiveOutcome.DONE) handleLaunchResponse(verdict.result.session);
}

function launchRecap() {
  return launch({
    key: 'recap',
    path: '/api/team-os/recap/launch',
    body: {},
    pending: 'Launching weekly recap…',
    icon: 'sprout',
    done: 'Launched weekly recap',
    failPrefix: 'Recap launch failed',
    unconfirmed: 'Recap launch not confirmed — check the session list before trying again.',
  });
}

// A row's tap follows the toolbar's Resume switch; the kebab's two launch
// items say which they want. Resume (the picker) drops the /<skill> prompt.
export function launchSkill(s, opts) {
  const picker = opts && typeof opts.picker === 'boolean' ? opts.picker : launchResume();
  return launch({
    key: 'skill-launch:' + s.id,
    path: '/api/team-os/skills/' + encodeURIComponent(s.id) + '/launch',
    body: { resume: picker },
    picker: picker,
    pending: (picker ? 'Opening the session picker for ' : 'Launching ') + s.name + '…',
    icon: picker ? 'clipboard-list' : 'sprout',
    done: picker ? 'Opening Copilot’s session picker for ' + s.name : 'Launched ' + s.name,
    failPrefix: 'Launch failed',
    unconfirmed: 'Launch of ' + s.name + ' not confirmed — check the session list before trying again.',
  });
}

// --------------------------------------------------------------- wire
export function wireTeamOs() {
  // No skills found: its fix is the Team OS folder.
  if (els.teamOsEmptyAction) {
    els.teamOsEmptyAction.addEventListener('click', function () {
      openSettingsAt('teamOsDir');
    });
  }
  wireContentBrowser();
  teamOsModelCombo = wireModelCombo(document.getElementById('teamOsModelCombo'));
  setTeamOsModelOptions();
  // Refresh skills + recap staleness the moment the tab opens (cheap: a live
  // directory scan + a single ledger stat).
  if (els.tabTeamOS) {
    els.tabTeamOS.addEventListener('click', function () {
      fetchSkills().catch(function () {});
      fetchRecapStatus().catch(function () {});
    });
  }
}
