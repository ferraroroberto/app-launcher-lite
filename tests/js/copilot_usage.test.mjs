// Unit pin for the Usage card's reading logic (copilot-usage.js): tone by
// pace, the pace words, the UTC reset label, an unknown plan never shown
// as 0%, and this machine's line kept beside it. Invoked by tests/unit/core/test_copilot_usage_js.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const m = await import('../../app/webapp/static/copilot-usage.js');

// Colour follows pace: under or on it accent, ahead attention, 90%+ danger.
assert.equal(m.usageTone(20, 50), 'accent');
assert.equal(m.usageTone(50, 50), 'accent');
assert.equal(m.usageTone(51, 50), 'attention');
assert.equal(m.usageTone(90, 99), 'danger');
assert.equal(m.usageTone(null, 50), 'none');

const ok = { state: 'ok', used_pct: 60, pace_pct: 40, used_credits: 180, allowance: 300,
  today_credits: 4.25, scope: 'this machine', source: 'a log', reset_at: '2026-12-01T00:00:00+00:00' };
assert.equal(m.paceSummary(ok), 'ahead of pace');
assert.equal(m.paceSummary({ ...ok, used_pct: 10 }), 'under pace');
assert.equal(m.paceSummary({ state: 'unknown' }), '');

// The reset day is the UTC one, whatever the viewer's zone.
assert.equal(m.resetLabel('2026-12-01T00:00:00+00:00'), 'resets Dec 1');
assert.equal(m.resetLabel('not a date'), '');

assert.equal(m.formatCredits(4.25), '4.3');
assert.equal(m.formatCredits(1234.4), '1,234');
assert.equal(m.footerText(ok), '4.3 credits today on this machine.');
// A plan nobody could read says so, and this machine's line still shows.
const planUnknown = { state: 'unknown', reason: 'no plan cached', today_credits: 2, allowance: 300,
  pace_pct: 40, source: 'a plan', scope: 'the plan' };
assert.equal(m.footerText(planUnknown), 'Plan usage unknown: no plan cached. 2 credits today on this machine.');
assert.equal(m.paceSummary(planUnknown), '');
assert.match(m.rowTitle(planUnknown), /Allowance from Settings: 300/);
assert.equal(m.footerText({ ...planUnknown, today_credits: null, today_state: 'unknown', today_reason: 'locked' }),
  'Plan usage unknown: no plan cached. This machine: unknown, locked.');
assert.equal(m.footerText({ state: 'pending' }), '–');
assert.match(m.rowTitle(ok), /180 of 300 AI credits used this month on the plan/);
assert.match(m.rowTitle({ ...ok, machine_month_credits: 12 }), /12 credits this month on this machine/);

// The details sheet writes the same figures out; an unreadable plan says
// unknown with its reason and never shows a used count.
assert.deepEqual(m.detailRows(ok).slice(0, 3), [
  ['Used this month', '180 of 300 AI credits'], ['Month gone', '40%'], ['Allowance', 'resets Dec 1']]);
assert.deepEqual(m.detailRows({ ...ok, machine_month_credits: 12 }).find(function (r) { return r[0] === 'This machine this month'; }),
  ['This machine this month', '12 credits']);
const unknownRows = m.detailRows(planUnknown);
assert.deepEqual(unknownRows[0], ['Plan usage', 'Unknown, no plan cached']);
assert.ok(!unknownRows.some(function (r) { return r[0] === 'Used this month'; }));
assert.deepEqual(m.detailRows({ state: 'pending' }), [['Plan usage', 'Still reading the plan']]);
assert.deepEqual(m.detailRows(null), [['Plan usage', 'Still reading the plan']]);

console.log('OK');
