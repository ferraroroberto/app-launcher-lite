/* The Team mirrors card on the Code tab and its two sheets.
 *
 * The server owns every rule (src/team_mirror/): the card lists what
 * GET /api/team-mirror says, the Link sheet saves a link, and the Publish
 * sheet shows a preview and publishes. A publish re-runs every check on the
 * server, so nothing the page shows is trusted there. The card reads only
 * while it is open: each check fetches every linked team repo.
 */

import { apiFailToast, jsonApi, toast } from '../api.js';
import { actionRow } from '../action-rows.js';
import { chip } from '../glance.js';
import { setAsyncState } from '../async-state.js';
import { plural, agoText } from '../shared/format.js';
import { terminalJsonApi } from '../webauthn.js';
import { startPoll } from '../_vendored/poll/poll.js';
import { setSwitch, switchEl } from '../_vendored/switch/switch.js';

const LIST_PATH = '/api/team-mirror';
// The server serves a row from its cache for 30 minutes; asking more often
// than that only re-reads the cache.
const POLL_MS = 30 * 60 * 1000;

const view = { rows: [], identity: null, refreshMinutes: 30, loaded: false, busy: false };

function projectPath(name, tail) {
  return '/api/team-mirror/' + encodeURIComponent(name) + (tail || '');
}

function linkPath(name) {
  return '/api/team-mirror/links/' + encodeURIComponent(name);
}

// The row's state as its context line and its one exception chip.
function describe(row) {
  const s = row.status || {};
  if (!row.link) return { meta: 'Not linked · tap to link a team repo', chips: [] };
  if (s.state === 'up_to_date') return { meta: 'Up to date', chips: [] };
  if (s.state === 'error') return { meta: 'Could not check the team repo', chips: [chip('unknown', 'danger')] };
  if (s.state === 'blocked') {
    const unknown = s.scan && s.scan.state === 'unknown';
    return { meta: unknown ? 'Privacy scan could not run' : 'Privacy scan found personal data', chips: [chip('blocked', 'danger')] };
  }
  const behind = s.behind == null ? 'Not published yet' : plural(s.behind, 'commit', 'commits') + ' behind';
  const team = s.team_changes ? [chip('team changed', 'attention')] : [];
  return { meta: behind, chips: team };
}

function publishable(row) {
  return row.link && row.status && row.status.state === 'behind' && !row.status.team_changes;
}

function render() {
  const list = document.getElementById('teamMirrorList');
  const meta = document.getElementById('teamMirrorSummaryMeta');
  const footer = document.getElementById('teamMirrorFooter');
  const all = document.getElementById('teamMirrorPublishAll');
  list.replaceChildren();
  view.rows.forEach(function (row) {
    const d = describe(row);
    const built = actionRow({
      id: row.project,
      className: 'team-mirror-item',
      title: row.project,
      meta: d.meta,
      chips: d.chips,
      label: row.link ? 'Preview the publish of ' + row.project : 'Link ' + row.project + ' to a team repo',
      onMain: function () { if (row.link) openPreview(row); else openLink(row); },
      kebabLabel: row.link ? 'Edit the link of ' + row.project : '',
    });
    if (built.kebab) {
      built.kebab.dataset.track = 'team-mirror.edit-link';
      built.kebab.addEventListener('click', function () { openLink(row); });
    }
    list.appendChild(built.li);
  });
  const ready = view.rows.filter(publishable);
  const behind = view.rows.filter(function (r) { return r.status && (r.status.state === 'behind' || r.status.state === 'blocked'); });
  meta.hidden = !behind.length;
  meta.textContent = behind.length ? String(behind.length) : '';
  all.disabled = view.busy || !ready.length;
  all.textContent = ready.length ? 'Publish all (' + ready.length + ')' : 'Publish all';
  const checked = view.rows.map(function (r) { return r.status && r.status.checked_at; }).filter(Boolean).sort()[0];
  footer.hidden = !checked;
  if (checked) footer.textContent = 'Checked ' + agoText(checked) + ' · refreshes every ' + view.refreshMinutes + ' min';
}

async function load(refresh) {
  const state = document.getElementById('teamMirrorState');
  if (!view.loaded) setAsyncState(state, 'loading', { message: 'Checking the team repos…' });
  try {
    const body = await jsonApi(LIST_PATH + (refresh ? '?refresh=1' : ''));
    view.rows = body.projects || [];
    view.identity = body.identity;
    view.refreshMinutes = body.refresh_minutes || 30;
    view.loaded = true;
    if (!view.rows.length) setAsyncState(state, 'empty', { message: 'No git projects in the projects folder.' });
    else setAsyncState(state, 'ready');
    render();
  } catch (exc) {
    if (view.loaded) setAsyncState(state, 'stale', { message: 'Last check failed · showing the previous one' });
    else setAsyncState(state, 'error', { message: 'Could not check the team repos.', retry: function () { load(true); } });
  }
}

// ------------------------------------------------------------- Link sheet

let linkRow = null;

function openLink(row) {
  linkRow = row;
  const link = row.link || {};
  document.getElementById('teamMirrorLinkApp').textContent = 'App: ' + row.project;
  document.getElementById('teamMirrorUrlInput').value = link.team_url || '';
  document.getElementById('teamMirrorBranchInput').value = link.branch || 'main';
  document.getElementById('teamMirrorLeaveOutInput').value = (link.leave_out || []).join('\n');
  document.getElementById('teamMirrorAuthor').textContent = view.identity
    ? 'Commit author: ' + view.identity.name + ' <' + view.identity.email + '>, from this computer\'s git config'
    : 'Commit author: not set. Set git\'s global user.name and user.email on this computer first.';
  document.getElementById('teamMirrorAccessResult').textContent = '';
  document.getElementById('teamMirrorRemove').hidden = !row.link;
  document.getElementById('teamMirrorLinkSheet').showModal();
}

function linkBody() {
  return {
    team_url: document.getElementById('teamMirrorUrlInput').value.trim(),
    branch: document.getElementById('teamMirrorBranchInput').value.trim() || 'main',
    leave_out: document.getElementById('teamMirrorLeaveOutInput').value.split('\n')
      .map(function (s) { return s.trim(); }).filter(Boolean),
  };
}

async function testAccess() {
  const out = document.getElementById('teamMirrorAccessResult');
  const btn = document.getElementById('teamMirrorTestAccess');
  btn.disabled = true;
  out.textContent = 'Testing…';
  try {
    const r = await terminalJsonApi('/api/team-mirror/test-access', { method: 'POST', body: linkBody() });
    out.textContent = r.ok ? 'The team repo answers and has that branch.' : r.error;
    out.dataset.tone = r.ok ? 'ok' : 'danger';
  } catch (exc) {
    out.textContent = exc.message || 'The test could not run.';
    out.dataset.tone = 'danger';
  } finally {
    btn.disabled = false;
  }
}

async function saveLink(event) {
  event.preventDefault();
  if (!linkRow) return;
  try {
    await terminalJsonApi(linkPath(linkRow.project), { method: 'PUT', body: linkBody() });
    document.getElementById('teamMirrorLinkSheet').close();
    toast('Linked ' + linkRow.project, 'ok');
    load(true);
  } catch (exc) {
    apiFailToast('Could not save the link', exc);
  }
}

async function removeLink() {
  if (!linkRow) return;
  try {
    await terminalJsonApi(linkPath(linkRow.project), { method: 'DELETE' });
    document.getElementById('teamMirrorLinkSheet').close();
    toast('Removed the link of ' + linkRow.project, 'ok');
    load(true);
  } catch (exc) {
    apiFailToast('Could not remove the link', exc);
  }
}

// ---------------------------------------------------------- Publish sheet

const pub = { row: null, preview: null, merge: true, confirm: false, mergeSwitch: null, confirmSwitch: null };

function fileLine(change) {
  const li = document.createElement('li');
  li.dataset.status = change.status;
  const name = document.createElement('span');
  name.className = 'git-sync-name';
  name.textContent = change.path;
  li.appendChild(name);
  const word = { A: 'added', M: 'changed', D: 'deleted' }[change.status] || change.status;
  li.appendChild(chip(word, change.status === 'D' ? 'attention' : 'neutral'));
  return li;
}

function renderScan(host, scan) {
  host.replaceChildren();
  host.dataset.state = scan.state;
  const head = document.createElement('p');
  head.className = 'team-mirror-scan-head';
  if (scan.state === 'clear') {
    head.textContent = 'Privacy scan: nothing found in ' + plural(scan.scanned, 'file', 'files') + '.';
    host.appendChild(head);
    return;
  }
  head.appendChild(chip('blocked', 'danger'));
  head.appendChild(document.createTextNode(scan.state === 'unknown'
    ? ' Privacy scan could not run, so nothing is published.'
    : ' Privacy scan found personal data. Fix these in the app, then preview again.'));
  host.appendChild(head);
  const list = document.createElement('ul');
  list.className = 'git-sync-body team-mirror-findings';
  scan.findings.forEach(function (f) {
    const li = document.createElement('li');
    li.textContent = f.path + ':' + f.line + ' · ' + f.rule;
    list.appendChild(li);
  });
  Object.keys(scan.unknown || {}).forEach(function (k) {
    const li = document.createElement('li');
    li.textContent = k + ' · ' + scan.unknown[k];
    list.appendChild(li);
  });
  host.appendChild(list);
}

function syncPublishButton() {
  const btn = document.getElementById('teamMirrorPublish');
  const p = pub.preview;
  btn.textContent = pub.merge ? 'Publish and merge' : 'Open PR only';
  btn.disabled = !p || !p.can_publish || (p.needs_confirmation && !pub.confirm);
}

function renderPreview(p) {
  pub.preview = p;
  document.getElementById('teamMirrorPreviewBody').hidden = false;
  const summary = document.getElementById('teamMirrorPreviewSummary');
  if (p.up_to_date) {
    summary.textContent = 'The team repo already matches this app.';
    document.getElementById('teamMirrorFiles').replaceChildren();
    document.getElementById('teamMirrorScan').replaceChildren();
    document.getElementById('teamMirrorTeamChanges').hidden = true;
    syncPublishButton();
    return;
  }
  const behind = p.behind == null ? 'first publish' : plural(p.behind, 'commit', 'commits') + ' since the last publish';
  summary.textContent = plural(p.changes.length, 'file changes', 'files change') + ' · ' + behind +
    (p.identity ? ' · as ' + p.identity.name : ' · no commit author set on this computer');
  const files = document.getElementById('teamMirrorFiles');
  files.replaceChildren();
  p.changes.forEach(function (c) { files.appendChild(fileLine(c)); });
  renderScan(document.getElementById('teamMirrorScan'), p.scan);
  const team = document.getElementById('teamMirrorTeamChanges');
  team.hidden = !p.needs_confirmation;
  const teamList = document.getElementById('teamMirrorTeamList');
  teamList.replaceChildren();
  (p.team_changes || []).forEach(function (c) {
    const li = document.createElement('li');
    li.textContent = c.sha + ' · ' + c.subject;
    teamList.appendChild(li);
  });
  document.getElementById('teamMirrorPrBody').value = p.pr_body || '';
  syncPublishButton();
}

async function openPreview(row) {
  pub.row = row;
  pub.preview = null;
  pub.confirm = false;
  pub.merge = true;
  setSwitch(pub.mergeSwitch, true);
  setSwitch(pub.confirmSwitch, false);
  document.getElementById('teamMirrorPreviewTitle').lastChild.textContent = ' Publish ' + row.project;
  document.getElementById('teamMirrorPreviewBody').hidden = true;
  document.getElementById('teamMirrorResult').hidden = true;
  const state = document.getElementById('teamMirrorPreviewState');
  setAsyncState(state, 'loading', { message: 'Comparing with the team repo…' });
  syncPublishButton();
  document.getElementById('teamMirrorPreviewSheet').showModal();
  try {
    const p = await terminalJsonApi(projectPath(row.project, '/preview'), { method: 'POST' });
    setAsyncState(state, 'ready');
    renderPreview(p);
  } catch (exc) {
    setAsyncState(state, 'error', { message: exc.message || 'Could not compare with the team repo.',
      retry: function () { openPreview(row); } });
  }
}

// What a publish's answer means, in the sheet's words; `done` when it ended the run.
function outcome(r) {
  if (r.result === 'merged') return { text: 'Published and merged.', done: true };
  if (r.result === 'pr_opened') return { text: 'PR opened, not merged: ' + (r.note || 'the host did not merge it') + '.', done: true, url: r.url };
  if (r.result === 'branch_pushed') return { text: 'Pushed to the branch ' + r.branch + '.', done: true };
  if (r.result === 'up_to_date') return { text: 'Already up to date.', done: true };
  if (r.result === 'blocked') return { text: 'The privacy scan stopped the publish.', done: false };
  if (r.result === 'needs_confirmation') return { text: 'The team repo changed: confirm replacing its changes.', done: false };
  return { text: r.error || 'Not published.', done: false };
}

function showResult(o) {
  const out = document.getElementById('teamMirrorResult');
  out.replaceChildren(document.createTextNode(o.text));
  if (o.url) {
    const a = document.createElement('a');
    a.className = 'reference-pill';
    a.href = o.url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Open the PR';
    out.appendChild(document.createTextNode(' '));
    out.appendChild(a);
  }
  out.hidden = false;
}

async function publishNow() {
  const row = pub.row;
  const btn = document.getElementById('teamMirrorPublish');
  if (!row) return;
  btn.disabled = true;
  btn.textContent = 'Publishing…';
  try {
    const r = await terminalJsonApi(projectPath(row.project, '/publish'), {
      method: 'POST',
      body: {
        mode: pub.merge ? 'merge' : 'pr_only',
        confirm_team_changes: pub.confirm,
        pr_body: document.getElementById('teamMirrorPrBody').value,
      },
    });
    const o = outcome(r);
    showResult(o);
    if (r.result === 'blocked' && r.scan) renderScan(document.getElementById('teamMirrorScan'), r.scan);
    toast(row.project + ': ' + o.text, o.done ? 'ok' : 'warn');
    if (o.done) { pub.preview = null; load(true); }
  } catch (exc) {
    showResult({ text: exc.message || 'The publish failed.' });
    apiFailToast('Publish failed', exc);
  } finally {
    syncPublishButton();
  }
}

// Publish all: every linked app that is behind with nothing to confirm, one at
// a time, merged. A blocked one or one the team changed waits for its own sheet.
async function publishAll() {
  const ready = view.rows.filter(publishable);
  if (!ready.length || view.busy) return;
  view.busy = true;
  render();
  let merged = 0;
  const stopped = [];
  for (const row of ready) {
    try {
      const r = await terminalJsonApi(projectPath(row.project, '/publish'), { method: 'POST', body: { mode: 'merge' } });
      if (outcome(r).done) merged += 1;
      else stopped.push(row.project);
    } catch (exc) {
      stopped.push(row.project);
    }
  }
  view.busy = false;
  toast('Published ' + merged + ' of ' + ready.length + (stopped.length ? ' · open ' + stopped.join(', ') + ' to see why' : ''),
    stopped.length ? 'warn' : 'ok');
  load(true);
}

export function wireTeamMirror() {
  const card = document.getElementById('teamMirrorCard');
  if (!card) return;
  pub.mergeSwitch = switchEl(true, {
    label: 'Merge after opening the PR',
    onToggle: function (next, btn) { pub.merge = next; setSwitch(btn, next); syncPublishButton(); },
  });
  pub.mergeSwitch.dataset.track = 'team-mirror-preview.merge';
  document.getElementById('teamMirrorMergeSlot').appendChild(pub.mergeSwitch);
  pub.confirmSwitch = switchEl(false, {
    label: 'Replace the team\'s changes',
    onToggle: function (next, btn) { pub.confirm = next; setSwitch(btn, next); syncPublishButton(); },
  });
  pub.confirmSwitch.dataset.track = 'team-mirror-preview.confirm-team';
  document.getElementById('teamMirrorConfirmSlot').appendChild(pub.confirmSwitch);

  const linkSheet = document.getElementById('teamMirrorLinkSheet');
  const previewSheet = document.getElementById('teamMirrorPreviewSheet');
  document.getElementById('teamMirrorLinkClose').addEventListener('click', function () { linkSheet.close(); });
  document.getElementById('teamMirrorLinkForm').addEventListener('submit', saveLink);
  document.getElementById('teamMirrorTestAccess').addEventListener('click', testAccess);
  document.getElementById('teamMirrorRemove').addEventListener('click', removeLink);
  document.getElementById('teamMirrorPreviewClose').addEventListener('click', function () { previewSheet.close(); });
  document.getElementById('teamMirrorPublish').addEventListener('click', publishNow);
  document.getElementById('teamMirrorRefresh').addEventListener('click', function () { load(true); });
  document.getElementById('teamMirrorPublishAll').addEventListener('click', publishAll);

  let poll = null;
  card.addEventListener('toggle', function () {
    if (card.open && !poll) {
      load(false);
      poll = startPoll(function () { return load(false); }, POLL_MS, { activeWhile: function () { return card.open; } });
    } else if (!card.open && poll) {
      poll.stop();
      poll = null;
    }
  });
}
