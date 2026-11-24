// Unit pin for the History sheet's reading of /api/progress: the totals, the
// series, the words a figure carries when it was not measured, and the range
// maths. Runs under plain Node against the real ES module. Invoked by
// tests/unit/progress/test_progress_model_js.py.

import assert from 'node:assert/strict';
import {
  bucketLabel, cellWord, fmtN, progressUrl, rangeDays, reposCaption, repoRows, series,
  sourceStatus, summary, tiles, total,
} from '../../app/webapp/static/progress/progress-model.js';

const data = {
  bucket: 'day', from: '2026-11-01', to: '2026-11-03',
  git: { state: 'ok', age_seconds: 240, refreshing: false },
  gitlab: { state: 'ok', age_seconds: 60, refreshing: false, skipped: [{ repo: 'local-only', reason: 'no GitLab remote' }], truncated: [] },
  buckets: [
    { start: '2026-11-01', end: '2026-11-01',
      git: { lines_added: 100, lines_removed: 40 },
      gitlab: { mrs_merged: 2, issues_opened: 3, issues_closed: 1 } },
    // A bucket GitLab could not read: a gap, not a zero. Lines are still known.
    { start: '2026-11-02', end: '2026-11-02', git: { lines_added: 7, lines_removed: 1 }, gitlab: null },
    { start: '2026-11-03', end: '2026-11-03',
      git: { lines_added: 50, lines_removed: 0 },
      gitlab: { mrs_merged: 1, issues_opened: 0, issues_closed: 2 } },
  ],
  repos: [
    { repo: 'alpha', git_state: 'ok', gitlab_state: 'ok', lines_added: 10, lines_removed: 5, mrs_merged: 1, issues_opened: 0, issues_closed: 0 },
    { repo: 'beta', git_state: 'ok', gitlab_state: 'stale', lines_added: 90, lines_removed: 35, mrs_merged: 2, issues_opened: 3, issues_closed: 3 },
    { repo: 'local-only', git_state: 'ok', gitlab_state: 'skipped', lines_added: 4, lines_removed: 0, mrs_merged: null, issues_opened: null, issues_closed: null },
    { repo: 'fresh', git_state: 'unknown', gitlab_state: 'unknown', lines_added: null, lines_removed: null, mrs_merged: null, issues_opened: null, issues_closed: null },
  ],
};

// Totals sum only the buckets that were read, per source.
assert.equal(total(data, 'lines_added'), 157);
assert.equal(total(data, 'mrs_merged'), 3);
assert.equal(total(data, 'issues_closed'), 3);
// ...and are null (unknown), never 0, when none was.
assert.equal(total({ buckets: [{ gitlab: null }, { gitlab: null }] }, 'mrs_merged'), null);
assert.equal(total({ buckets: [] }, 'lines_added'), null);
// A real zero stays a zero.
assert.equal(total({ buckets: [{ gitlab: { mrs_merged: 0 } }] }, 'mrs_merged'), 0);
// One source unread leaves the other's figures standing.
assert.equal(total({ buckets: [{ git: null, gitlab: { mrs_merged: 4 } }] }, 'lines_added'), null);
assert.equal(total({ buckets: [{ git: null, gitlab: { mrs_merged: 4 } }] }, 'mrs_merged'), 4);

// The chart series keeps the unread bucket as null.
const points = series(data, ['lines_added', 'mrs_merged']);
assert.equal(points.length, 3);
assert.deepEqual(points[1], { k: '2026-11-02', lines_added: 7, mrs_merged: null });
assert.equal(points[2].lines_added, 50);

// The three tiles, and which source each reads from.
const t = tiles(data);
assert.deepEqual(t.map((x) => x.key), ['lines', 'mrs', 'issues']);
assert.deepEqual(t.map((x) => x.source), ['git', 'gitlab', 'gitlab']);
assert.deepEqual(t[0].pair, [157, 41]);
assert.deepEqual(t[1].pair, [3]);
assert.deepEqual(t[2].pair, [3, 3]);

// Project rows: largest first by the chosen column; a project with no figure
// there sorts after every one that has; a bad key falls back to MRs.
assert.deepEqual(repoRows(data, 'lines_added').map((r) => r.repo), ['beta', 'alpha', 'local-only', 'fresh']);
assert.deepEqual(repoRows(data, 'nonsense').map((r) => r.repo), ['beta', 'alpha', 'fresh', 'local-only']);
assert.deepEqual(repoRows({}, 'mrs_merged'), []);

// A cell with no figure says why, never "0".
assert.equal(cellWord(data.repos[2], 'mrs_merged'), 'skipped');
assert.equal(cellWord(data.repos[2], 'lines_added'), '');
assert.equal(cellWord(data.repos[3], 'lines_added'), 'unknown');
assert.equal(cellWord(data.repos[3], 'issues_opened'), 'unknown');
assert.equal(cellWord(data.repos[0], 'mrs_merged'), '');
assert.equal(reposCaption(data), '4 projects · 1 without a GitLab remote');
assert.equal(reposCaption({ repos: [{}] }), '1 project');

// A source's words.
assert.equal(sourceStatus({ state: 'ok' }).word, '');
assert.equal(sourceStatus({ state: 'stale' }).readable, true);
assert.equal(sourceStatus({ state: 'unavailable' }).readable, false);
assert.equal(sourceStatus({ state: 'unavailable' }).word, 'unavailable');
assert.equal(sourceStatus({ state: 'unknown' }).word, 'unknown');
assert.equal(sourceStatus(null).state, 'unknown');
assert.equal(sourceStatus({ state: 'surprise' }).state, 'unknown');
assert.equal(sourceStatus({ state: 'ok', age_seconds: 240 }).age, '4 min ago');
assert.deepEqual(sourceStatus(data.gitlab).skipped, [{ repo: 'local-only', reason: 'no GitLab remote' }]);
assert.equal(summary({ git: { state: 'stale' }, gitlab: { state: 'ok' } }).stale, true);
assert.equal(summary({ git: { state: 'ok' }, gitlab: { state: 'stale' } }).stale, true);
assert.equal(summary({ git: { state: 'ok' }, gitlab: { state: 'ok' }, refreshing: true }).refreshing, true);

// Numbers.
assert.equal(fmtN(null), '–');
assert.equal(fmtN(0), '0');
assert.equal(fmtN(999), '999');
assert.equal(fmtN(12345), '12.3k');
assert.equal(fmtN(2_500_000), '2.5M');

// Ranges: the last N days including today; a custom pair must be usable.
const today = new Date(2026, 10, 4);
assert.deepEqual(rangeDays('30', null, today), { from: '2026-10-06', to: '2026-11-04', valid: true });
assert.deepEqual(rangeDays('custom', { from: '2026-10-01', to: '2026-10-31' }, today), { from: '2026-10-01', to: '2026-10-31', valid: true });
assert.equal(rangeDays('custom', { from: '2026-11-02', to: '2026-11-01' }, today).valid, false);
assert.equal(rangeDays('custom', { from: '', to: '' }, today).valid, false);
assert.equal(rangeDays('custom', { from: '2025-01-01', to: '2026-11-01' }, today).valid, false);
assert.equal(rangeDays('custom', { from: '2025-01-01', to: '2026-11-01' }, today).to, '2026-11-04');

assert.equal(progressUrl('week', '2026-10-01', '2026-10-31', false), '/api/progress?bucket=week&from=2026-10-01&to=2026-10-31');
assert.ok(progressUrl('day', '2026-10-01', '2026-10-31', true).endsWith('&refresh=true'));

// Labels.
assert.equal(bucketLabel('2026-11-04', 'day'), '4 Nov');
assert.equal(bucketLabel('2026-11-01', 'month'), 'Nov');


// --- the refresh toast and its summary
import {
  lastRefreshedText, refreshProgressText, refreshSummary,
} from '../../app/webapp/static/progress/progress-model.js';

assert.equal(refreshProgressText(null), 'Checking projects…');
assert.equal(refreshProgressText({ done: 0, total: null }), 'Checking projects…');
assert.equal(refreshProgressText({ done: 0, total: 26 }), 'Checking 1 of 26');
assert.equal(refreshProgressText({ done: 1, total: 26 }), 'Checking 2 of 26');
assert.equal(refreshProgressText({ done: 26, total: 26 }), 'Checking 26 of 26');

const ran = (results, extra) => ({ state: 'done', results: results, ...extra });
const okRow = { status: 'ok' };
const skipRow = { status: 'skipped', reason: 'no GitLab remote' };

// A skip is counted and neutral, never an error.
const skipped = refreshSummary(ran({ a: okRow, b: okRow, c: skipRow }));
assert.equal(skipped.text, 'Refreshed 2 of 3 · 1 skipped (no GitLab remote)');
assert.equal(skipped.kind, '');
assert.deepEqual(skipped.failed, []);

// A real read failure is the error style and names the project.
const failed = refreshSummary(ran({ a: okRow, b: { status: 'failed', error: 'gitlab: 401' }, c: skipRow, d: { status: 'failed', error: 'git: x' } }));
assert.equal(failed.kind, 'error');
assert.deepEqual(failed.failed, ['b', 'd']);
assert.equal(failed.text, 'Refreshed 1 of 4 · 1 skipped (no GitLab remote) · could not read b, d');

assert.equal(refreshSummary(ran({ a: okRow })).text, 'Refreshed 1 of 1');
assert.equal(refreshSummary(ran({})).kind, '');
assert.equal(refreshSummary(ran({ a: okRow }, { error: 'OSError: gone' })).kind, 'error');

assert.equal(lastRefreshedText({}), 'Not refreshed yet. Tap Refresh to read the history.');
assert.equal(lastRefreshedText({ git: { age_seconds: 4000 }, gitlab: { age_seconds: 200 } }), 'Last refreshed 3 min ago. Tap Refresh to update.');
assert.equal(lastRefreshedText({ git: { age_seconds: 4000 }, gitlab: { age_seconds: null } }), 'Last refreshed 1 h ago. Tap Refresh to update.');

console.log('OK');
