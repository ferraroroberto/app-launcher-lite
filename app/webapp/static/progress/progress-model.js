/* The History sheet's reading of GET /api/progress, with no DOM.
 *
 * Everything the sheet shows is worked out here from the response, so
 * tests/js/progress_model.test.mjs pins it under plain Node: the totals, the
 * series each chart draws, the per-project rows, and the words a source or a
 * cell carries when it was not measured.
 *
 * A figure that was not measured is `null`, never 0: a bucket a source could
 * not read is a gap in its chart, a total over no readable bucket reads
 * "unknown", and a project with no GitLab remote reads "skipped".
 */

import { agoFromSeconds } from '../shared/format.js';

export const BUCKETS = ['day', 'week', 'month'];

// The range choices: a number is the last N days (today included).
export const RANGES = [
  { key: '30', label: '30d', days: 30 },
  { key: '90', label: '90d', days: 90 },
  { key: '180', label: '6m', days: 180 },
  { key: 'custom', label: 'Custom', days: null },
];
export const DEFAULT_RANGE = '30';
// The route's own cap on a range.
export const MAX_RANGE_DAYS = 400;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// ------------------------------------------------------------- numbers

// 12 345 -> "12.3k". null is "–" so a cell never reads as a zero it is not.
export function fmtN(n) {
  if (n == null || typeof n !== 'number' || isNaN(n)) return '–';
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1) + 'B';
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M';
  if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'k';
  return n.toLocaleString('en-US');
}

// ---------------------------------------------------------------- dates

function pad(n) { return String(n).padStart(2, '0'); }

// A local calendar day as YYYY-MM-DD.
export function isoDay(date) {
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
}

function parseDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

function shiftDays(date, n) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  d.setDate(d.getDate() + n);
  return d;
}

// The first and last day a range choice asks for. A custom range that is not
// a usable pair of days reads as the default window, so the sheet never sends
// a request the route would refuse.
export function rangeDays(rangeKey, custom, today) {
  const end = today || new Date();
  const choice = RANGES.find(function (r) { return r.key === rangeKey; });
  if (choice && choice.days) {
    return { from: isoDay(shiftDays(end, 1 - choice.days)), to: isoDay(end), valid: true };
  }
  const from = parseDay(custom && custom.from);
  const to = parseDay(custom && custom.to);
  if (from && to && from <= to && Math.round((to - from) / 864e5) + 1 <= MAX_RANGE_DAYS) {
    return { from: isoDay(from), to: isoDay(to), valid: true };
  }
  return { from: isoDay(shiftDays(end, -29)), to: isoDay(end), valid: false };
}

export function progressUrl(bucket, from, to, refresh) {
  let url = '/api/progress?bucket=' + encodeURIComponent(bucket) +
    '&from=' + encodeURIComponent(from) + '&to=' + encodeURIComponent(to);
  if (refresh) url += '&refresh=true';
  return url;
}

// "4 Nov" for an axis; a month bucket is just "Nov".
export function bucketLabel(start, bucket) {
  const d = parseDay(start);
  if (!d) return '';
  return bucket === 'month' ? MONTHS[d.getMonth()] : d.getDate() + ' ' + MONTHS[d.getMonth()];
}

// The tooltip's title for a bucket.
export function bucketTitle(start, bucket) {
  const d = parseDay(start);
  if (!d) return '';
  if (bucket === 'month') return MONTHS[d.getMonth()] + ' ' + d.getFullYear();
  if (bucket === 'week') return 'Week of ' + d.getDate() + ' ' + MONTHS[d.getMonth()];
  return WEEKDAYS[d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()];
}

// "4 Nov – 3 Dec 2026".
export function rangeText(from, to) {
  const a = parseDay(from);
  const b = parseDay(to);
  if (!a || !b) return '';
  return a.getDate() + ' ' + MONTHS[a.getMonth()] + ' – ' + b.getDate() + ' ' + MONTHS[b.getMonth()] + ' ' + b.getFullYear();
}

// ---------------------------------------------------- the two sources

export const SOURCES = ['git', 'gitlab'];

const STATE_WORDS = {
  ok: '',
  stale: 'stale',
  unknown: 'unknown',
  unavailable: 'unavailable',
};

// What a source (local git, GitLab) tells the reader: `word` is the state word
// to show when it is not ok, `readable` whether any figure of it can be drawn,
// `age` when it was last read.
export function sourceStatus(source) {
  const s = source || {};
  const state = STATE_WORDS[s.state] === undefined ? 'unknown' : s.state;
  return {
    state: state,
    word: STATE_WORDS[state],
    readable: state === 'ok' || state === 'stale',
    reason: s.reason || '',
    age: agoFromSeconds(s.age_seconds),
    refreshing: !!s.refreshing,
    skipped: Array.isArray(s.skipped) ? s.skipped : [],
    truncated: Array.isArray(s.truncated) ? s.truncated : [],
  };
}

// Whether the sheet's data is being refreshed, and whether anything on it is
// older than a fresh read.
export function summary(data) {
  const d = data || {};
  const git = sourceStatus(d.git);
  const gitlab = sourceStatus(d.gitlab);
  return {
    git: git,
    gitlab: gitlab,
    refreshing: !!d.refreshing,
    stale: git.state === 'stale' || gitlab.state === 'stale',
  };
}

// ------------------------------------------------------------- totals

function bucketsOf(data) {
  return data && Array.isArray(data.buckets) ? data.buckets : [];
}

const SOURCE_OF = {
  lines_added: 'git', lines_removed: 'git',
  mrs_merged: 'gitlab', issues_opened: 'gitlab', issues_closed: 'gitlab',
};

function valueOf(bucket, metric) {
  const part = bucket[SOURCE_OF[metric]];
  return part && typeof part[metric] === 'number' ? part[metric] : null;
}

// One metric summed over the buckets that were read; null when none was, so an
// unread range is "unknown" and not a total of zero.
export function total(data, metric) {
  let sum = null;
  bucketsOf(data).forEach(function (b) {
    const v = valueOf(b, metric);
    if (v != null) sum = (sum || 0) + v;
  });
  return sum;
}

// The three headline tiles. A value is a number or null (unknown); the view
// puts the word beside a null.
export function tiles(data) {
  return [
    {
      key: 'lines', label: 'Lines of code', glyph: 'code-xml', source: 'git',
      pair: [total(data, 'lines_added'), total(data, 'lines_removed')],
    },
    { key: 'mrs', label: 'MRs merged', glyph: 'git-pull-request', source: 'gitlab', pair: [total(data, 'mrs_merged')] },
    {
      key: 'issues', label: 'Issues', glyph: 'circle-check', source: 'gitlab',
      pair: [total(data, 'issues_opened'), total(data, 'issues_closed')],
    },
  ];
}

// -------------------------------------------------------------- series

// One chart point per bucket: `k` the bucket's start, and each series' value
// or null for a bucket that was not read.
export function series(data, metrics) {
  return bucketsOf(data).map(function (b) {
    const point = { k: b.start };
    metrics.forEach(function (m) { point[m] = valueOf(b, m); });
    return point;
  });
}

// ----------------------------------------------------------- tables

export const REPO_SORTS = ['lines_added', 'lines_removed', 'mrs_merged', 'issues_opened', 'issues_closed'];

// The per-project rows, the largest first by the chosen column. A project whose
// source was not read has no figure there and sorts after every one that has.
export function repoRows(data, sortKey) {
  const key = REPO_SORTS.indexOf(sortKey) === -1 ? 'mrs_merged' : sortKey;
  const rows = data && Array.isArray(data.repos) ? data.repos.slice() : [];
  const rank = function (r) { return typeof r[key] === 'number' ? r[key] : -1; };
  return rows.sort(function (a, b) {
    return rank(b) - rank(a) || String(a.repo).localeCompare(String(b.repo));
  });
}

// The word a cell carries when it has no figure: why that project's source was
// not read ("skipped" when it has no GitLab remote), '' when it has one.
export function cellWord(row, metric) {
  if (typeof row[metric] === 'number') return '';
  return row[SOURCE_OF[metric] + '_state'] === 'skipped' ? 'skipped' : 'unknown';
}

// ------------------------------------------------------------- refresh

// The toast text while a refresh runs: the project being checked, counted from
// 1. Before the server has listed the projects there is no total to count to.
export function refreshProgressText(refresh) {
  const total = refresh && typeof refresh.total === 'number' ? refresh.total : null;
  if (!total) return 'Checking projects…';
  const done = typeof refresh.done === 'number' ? refresh.done : 0;
  return 'Checking ' + Math.min(done + 1, total) + ' of ' + total;
}

// How a finished refresh ends: the toast's `text` and `kind` ('error' only when
// a read really failed, or the refresh itself stopped). A project with no
// remote is a skip: it is counted in the summary and never turns it red.
export function refreshSummary(refresh) {
  const r = refresh || {};
  const results = r.results && typeof r.results === 'object' ? r.results : {};
  const names = Object.keys(results).sort();
  // A failed read carries its `error`, a skip its `reason`; a project that was read carries neither.
  const failed = names.filter(function (n) { return !!results[n].error; });
  const skipped = names.filter(function (n) { return !results[n].error && !!results[n].reason; });
  const readOk = names.length - failed.length - skipped.length;
  if (r.error) return { text: 'Could not refresh: ' + r.error, kind: 'error', failed: failed, skipped: skipped };
  if (!names.length) return { text: 'No projects to refresh.', kind: '', failed: failed, skipped: skipped };
  let text = 'Refreshed ' + readOk + ' of ' + names.length;
  if (skipped.length) text += ' · ' + skipped.length + ' skipped (' + (results[skipped[0]].reason || 'no GitLab remote') + ')';
  if (failed.length) text += ' · could not read ' + failed.join(', ');
  return { text: text, kind: failed.length ? 'error' : '', failed: failed, skipped: skipped };
}

// The line under the range: when the saved figures were last read, from the
// younger of the two sources, or the nudge to read them once.
export function lastRefreshedText(data) {
  const d = data || {};
  const ages = [d.git, d.gitlab]
    .map(function (s) { return s && typeof s.age_seconds === 'number' ? s.age_seconds : null; })
    .filter(function (a) { return a != null; });
  if (!ages.length) return 'Not refreshed yet. Tap Refresh to read the history.';
  return 'Last refreshed ' + agoFromSeconds(Math.min.apply(null, ages)) + '. Tap Refresh to update.';
}

// "3 projects" over the rows the range has, with how many were not read.
export function reposCaption(data) {
  const rows = data && Array.isArray(data.repos) ? data.repos : [];
  const skipped = data && data.gitlab && Array.isArray(data.gitlab.skipped) ? data.gitlab.skipped.length : 0;
  return rows.length + (rows.length === 1 ? ' project' : ' projects') +
    (skipped ? ' · ' + skipped + ' without a GitLab remote' : '');
}
