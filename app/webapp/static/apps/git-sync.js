/* The Projects card's remote side: fetch, Pull, Sync all and the pull-first
 * gate in front of a launch on a repo that is behind.
 *
 * The server owns every rule (src/git_upstream.py): only a clean repo on its
 * default branch that is only behind is fast-forwarded, every other one is
 * skipped with its reason. This module asks and reports; it decides nothing
 * the server would not.
 */

import { state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { chip } from '../glance.js';
import { confirmDialog } from '../confirm-dialog.js';
import { setAsyncState } from '../async-state.js';
import { plural } from '../shared/format.js';
import { hideToast } from '../_vendored/toast/toast.js';
import { startPoll } from '../_vendored/poll/poll.js';


// A repo the server would fast-forward: clean, on its default branch, behind
// and not ahead.
export function canPull(gs) {
  const up = gs && gs.upstream;
  return !!(up && up.behind > 0 && !up.ahead && !gs.dirty && gs.on_default_branch && gs.branch);
}

// Why a behind repo cannot be pulled from here, in the words the sheet uses.
function whyNotPullable(gs) {
  if (gs.dirty) return 'it has uncommitted changes';
  if (!gs.branch || !gs.on_default_branch) return 'it is not on ' + (gs.default_branch || 'its default branch');
  if (gs.upstream.ahead) return 'it has local commits too (diverged)';
  return 'the server would skip it';
}

// The fetch behind the Projects refresh: every repo's origin, then the
// caller re-reads git status. A failed fetch shows on its tile as unknown.
export async function fetchAll() {
  const body = await jsonApi('/api/claude-code/git-fetch', { method: 'POST' });
  if (body && body.failed) toast(plural(body.failed, 'repo', 'repos') + ' could not be fetched', 'warn');
  return body;
}

// Fast-forward one project. Resolves true only when it ends up current.
export async function pullProject(a) {
  try {
    const r = await jsonApi('/api/claude-code/pull/' + encodeURIComponent(a.id), { method: 'POST' });
    if (r.outcome === 'pulled') {
      toast('Pulled ' + plural(r.commits, 'commit', 'commits') + ' into ' + a.name, 'good');
      return true;
    }
    if (r.outcome === 'up-to-date') {
      toast(a.name + ' is already up to date', 'ok');
      return true;
    }
    toast('Did not pull ' + a.name + ': ' + r.reason, 'warn');
    return false;
  } catch (exc) {
    apiFailToast('Pull failed', exc);
    return false;
  }
}

// In front of a Coding launch. Resolves null to cancel, else the launch's
// extra payload: {} for a repo that is not behind (or was just pulled), or
// {ignore_behind: true} when the owner chose to launch on a behind repo that
// cannot be pulled here. The server refuses the launch either way without it.
export async function behindGate(a) {
  const gs = state.gitStatus && state.gitStatus[a.id];
  const up = gs && gs.upstream;
  if (!up || !(up.behind > 0)) return {};
  const branch = gs.default_branch || 'main';
  const lag = branch + ' is ' + plural(up.behind, 'commit', 'commits') + ' behind origin/' + branch + '.';
  if (canPull(gs)) {
    const ok = await confirmDialog({
      title: 'Pull ' + a.name + ' first?',
      message: lag + ' Pull it, then launch.',
      action: 'Pull and launch',
      tone: 'primary',
    });
    if (!ok) return null;
    return (await pullProject(a)) ? {} : null;
  }
  const anyway = await confirmDialog({
    title: a.name + ' is behind',
    message: lag + ' It cannot be pulled here: ' + whyNotPullable(gs) + '.',
    action: 'Launch anyway',
  });
  return anyway ? { ignore_behind: true } : null;
}

// The server's reasons, in the words the report uses; any other reason shows as sent.
function reasonWord(reason) {
  return reason === 'dirty' ? 'uncommitted changes' : (reason || 'skipped');
}

const OUTCOME_CHIP = {
  pulled: function (r) { return chip('pulled ' + r.commits, 'accent'); },
  'up-to-date': function () { return chip('up to date', 'neutral'); },
  skipped: function (r) { return chip(reasonWord(r.reason), 'attention'); },
  failed: function (r) { return chip('failed: ' + r.reason, 'danger'); },
};

function renderResults(host, results) {
  host.replaceChildren();
  results.slice().sort(function (x, y) { return x.name.localeCompare(y.name); }).forEach(function (r) {
    const li = document.createElement('li');
    li.dataset.outcome = r.outcome;
    const name = document.createElement('span');
    name.className = 'git-sync-name';
    name.textContent = r.name;
    li.appendChild(name);
    li.appendChild((OUTCOME_CHIP[r.outcome] || OUTCOME_CHIP.skipped)(r));
    host.appendChild(li);
  });
}

// How many repos ended each way, with the skip reasons counted ("uncommitted changes (2)").
function tally(results) {
  const t = { pulled: 0, current: 0, skipped: 0, failed: 0, reasons: [] };
  const byReason = {};
  results.forEach(function (r) {
    if (r.outcome === 'pulled') t.pulled += 1;
    else if (r.outcome === 'up-to-date') t.current += 1;
    else if (r.outcome === 'failed') t.failed += 1;
    else {
      t.skipped += 1;
      const word = reasonWord(r.reason);
      byReason[word] = (byReason[word] || 0) + 1;
    }
  });
  t.reasons = Object.keys(byReason).sort().map(function (w) { return w + ' (' + byReason[w] + ')'; });
  return t;
}

// The report's head: one line per way a repo can end. Skipped and failed show
// only when there are some; the skip reasons ride under Skipped.
function renderSummary(host, t) {
  host.replaceChildren();
  const rows = [
    ['pulled', 'Fast-forwarded', t.pulled, 'accent', ''],
    ['current', 'Already current', t.current, 'neutral', ''],
  ];
  if (t.skipped) rows.push(['skipped', 'Skipped', t.skipped, 'attention', t.reasons.join(', ')]);
  if (t.failed) rows.push(['failed', 'Failed', t.failed, 'danger', '']);
  rows.forEach(function (row) {
    const li = document.createElement('li');
    li.dataset.summary = row[0];
    const label = document.createElement('span');
    label.className = 'git-sync-name';
    label.textContent = row[1];
    li.appendChild(label);
    li.appendChild(chip(String(row[2]), row[3]));
    if (row[4]) {
      const why = document.createElement('span');
      why.className = 'git-sync-reasons';
      why.textContent = row[4];
      li.appendChild(why);
    }
    host.appendChild(li);
  });
  host.hidden = false;
}

// "Syncing 3 of 26 · name": the project being synced, counted from 1. Before
// the server has listed the repos there is no total to count to.
function progressText(p) {
  if (!p || typeof p.total !== 'number' || !p.total) return 'Syncing projects…';
  const text = 'Syncing ' + Math.min(p.done + 1, p.total) + ' of ' + p.total;
  return p.current ? text + ' · ' + p.current : text;
}

// The toast that outlives the sheet: the same count while it runs, the totals
// when it ends.
function summaryText(t, total) {
  let text = 'Synced ' + total + ': ' + t.pulled + ' fast-forwarded · ' + t.current + ' current';
  if (t.skipped) text += ' · ' + t.skipped + ' skipped';
  if (t.failed) text += ' · ' + t.failed + ' failed';
  return text;
}

let syncing = false;
const POLL_MS = 1000;

// Sync all: fetch every repo and fast-forward the safe ones. The sheet counts
// the projects while it runs (the server answers the POST when the run ends,
// so a GET beside it reports how far it is), then keeps the report until Done.
// `onDone` re-reads git status.
export async function syncAll(onDone) {
  const sheet = document.getElementById('gitSyncSheet');
  if (!sheet || syncing) return;
  const status = document.getElementById('gitSyncState');
  const summary = document.getElementById('gitSyncSummary');
  const body = document.getElementById('gitSyncBody');
  syncing = true;
  body.replaceChildren();
  summary.replaceChildren();
  summary.hidden = true;
  setAsyncState(status, 'loading', { message: progressText(null) });
  toast(progressText(null), '', { icon: 'refresh-cw', ms: 0 });
  if (!sheet.open) sheet.showModal();
  const poll = startPoll(async function () {
    const p = await jsonApi('/api/claude-code/sync-all');
    // Only a run that has started and not ended is this one: the last one, finished, or none yet, says nothing about it.
    if (!syncing || !p.started_at || p.finished_at) return;
    setAsyncState(status, 'loading', { message: progressText(p) });
    toast(progressText(p), '', { icon: 'refresh-cw', ms: 0 });
  }, POLL_MS, { activeWhile: function () { return syncing; } });
  try {
    const reply = await jsonApi('/api/claude-code/sync-all', { method: 'POST' });
    const results = reply.results || [];
    syncing = false;
    poll.stop();
    const t = tally(results);
    if (!results.length) {
      setAsyncState(status, 'empty', { message: 'No git repos found.' });
      hideToast();
    } else {
      setAsyncState(status, 'ready');
      renderSummary(summary, t);
      renderResults(body, results);
      // Open, the sheet is the report and a toast would sit under it; closed, the
      // toast carries the totals and opens the report.
      if (sheet.open) hideToast();
      else {
        toast(summaryText(t, results.length), t.failed ? 'error' : '', {
          icon: 'refresh-cw',
          action: { label: 'Report', onClick: function () { if (!sheet.open) sheet.showModal(); } },
        });
      }
    }
  } catch (exc) {
    setAsyncState(status, 'error', { message: 'Sync all failed: ' + exc.message });
    hideToast();
  } finally {
    syncing = false;
    poll.stop();
    if (onDone) onDone();
  }
}

export function wireGitSync() {
  const sheet = document.getElementById('gitSyncSheet');
  if (!sheet) return;
  const close = function () { if (sheet.open) sheet.close(); };
  document.getElementById('gitSyncClose').addEventListener('click', close);
  document.getElementById('gitSyncDone').addEventListener('click', close);
}
