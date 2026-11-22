// Unit pin for the Performance settings' reach into the page's polls
// (shared/poll.js, shared/perf-config.js): a spacing read at each re-arm, a
// hidden tab that keeps polling when the owner turns the pause off, and
// config values read live with the shipped default until config has loaded.
// Invoked by tests/unit/core/test_poll_spacing_js.py.

import assert from 'node:assert/strict';
import '../_browser_stubs.mjs';

const { state } = await import('../../../app/webapp/static/state.js');
const perf = await import('../../../app/webapp/static/shared/perf-config.js');
const { startPoll } = await import('../../../app/webapp/static/shared/poll.js');

// perf-config: the default stands in until config loads, then the saved value rules.
state.config = null;
assert.equal(perf.pollMs('poll_board_s', 5000)(), 5000);
assert.equal(perf.pauseWhenHidden(), true);
state.config = { performance: { values: { poll_board_s: 15, poll_pause_hidden: false } } };
assert.equal(perf.pollMs('poll_board_s', 5000)(), 15000);
assert.equal(perf.pauseWhenHidden(), false);

// A fake page: timers are recorded, and a test fires the armed one by hand.
function fakePage(hidden) {
  const page = { hidden, timers: [], listeners: [] };
  globalThis.document = {
    get hidden() { return page.hidden; },
    addEventListener: (type, fn) => page.listeners.push([type, fn]),
  };
  globalThis.window = {
    setTimeout: (fn, ms) => { page.timers.push({ fn, ms }); return page.timers.length; },
    clearTimeout: () => {},
  };
  return page;
}

async function fire(page) {
  const armed = page.timers.pop();
  await armed.fn();
}

// The spacing function is asked at every re-arm, so a saved change reaches a running poll.
{
  const page = fakePage(false);
  let ms = 3000;
  let reads = 0;
  startPoll(async () => { reads += 1; }, () => ms);
  assert.equal(page.timers[0].ms, 3000);
  ms = 9000;
  await fire(page);
  assert.equal(reads, 1);
  assert.equal(page.timers[0].ms, 9000, 'the re-arm used the new spacing');
}

// A plain number still works.
{
  const page = fakePage(false);
  startPoll(async () => {}, 4000);
  await fire(page);
  assert.equal(page.timers[0].ms, 4000);
}

// A hidden tab asks for nothing by default and arms nothing.
{
  const page = fakePage(true);
  let reads = 0;
  startPoll(async () => { reads += 1; }, 1000);
  await fire(page);
  assert.equal(reads, 0);
  assert.equal(page.timers.length, 0);
}

// With the pause turned off a hidden tab keeps polling.
{
  const page = fakePage(true);
  let reads = 0;
  startPoll(async () => { reads += 1; }, 1000, { pauseHidden: () => false });
  await fire(page);
  assert.equal(reads, 1);
  assert.equal(page.timers[0].ms, 1000);
}

console.log('OK');
