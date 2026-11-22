// Unit pin for the Usage card's reading logic (copilot-usage.js): tone by
// pace, the pace words, the UTC reset label, and an unknown reading never
// shown as 0%. Invoked by tests/unit/core/test_copilot_usage_js.py.

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
assert.equal(m.footerText({ state: 'unknown', reason: 'no Copilot usage log on this machine' }),
  'Unknown: no Copilot usage log on this machine.');
assert.equal(m.footerText({ state: 'pending' }), '–');
assert.match(m.rowTitle(ok), /180 of 300 AI credits/);
assert.match(m.rowTitle(ok), /this machine only/);

console.log('OK');
