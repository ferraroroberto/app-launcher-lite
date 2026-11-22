/* The page's live polls: one read in flight per shape, nothing while hidden.
 *
 * A phone page that overlaps its own reads fills the server's worker pools on
 * a slow computer, so every poll here follows three rules:
 *
 *   - singleFlight(): a caller that asks while the last read is still out
 *     shares that read instead of starting a second one.
 *   - startPoll(): the next tick is scheduled when the last one has settled,
 *     so a slow server stretches the gap instead of stacking requests.
 *   - A hidden tab or locked screen arms no tick. Coming back reads once,
 *     straight away, then resumes the normal spacing.
 */

// Wrap a read so concurrent callers share its one in-flight promise.
// `.fresh()` is for a caller that just wrote: a read already out may predate
// the write, so it waits for that one and then shares the next.
export function singleFlight(read) {
  let inFlight = null;
  function shared() {
    if (inFlight) return inFlight;
    let p;
    try {
      p = Promise.resolve(read());
    } catch (exc) {
      return Promise.reject(exc);
    }
    inFlight = p;
    p.finally(function () {
      if (inFlight === p) inFlight = null;
    }).catch(function () {});
    return p;
  }
  shared.fresh = function () {
    if (!inFlight) return shared();
    return inFlight.catch(function () {}).then(shared);
  };
  return shared;
}

// Poll `read` every `ms` after it settles, paused while the page is hidden.
// `ms` is a number or a function returning one, asked at each re-arm so a
// Settings > Performance change reaches a running poll. `opts.pauseHidden`
// is a function returning false to keep a hidden tab polling (default: pause).
export function startPoll(read, ms, opts) {
  const spacing = typeof ms === 'function' ? ms : function () { return ms; };
  const pauses = (opts && opts.pauseHidden) || function () { return true; };
  let timer = null;
  let running = false;

  function asleep() { return document.hidden && pauses(); }

  async function tick() {
    window.clearTimeout(timer);
    timer = null;
    if (asleep()) return;
    running = true;
    try {
      await read();
    } catch (_) {
      // Each read reports its own failure; the poll just asks again later.
    } finally {
      running = false;
    }
    // Hidden mid-read: arm nothing; the return to the foreground reads.
    if (!asleep()) timer = window.setTimeout(tick, spacing());
  }
  // The e2e fake clock names a poll by the read it runs.
  tick.pollSource = String(read);

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      if (pauses()) {
        window.clearTimeout(timer);
        timer = null;
      }
    } else if (!running) {
      tick();
    }
  });
  timer = window.setTimeout(tick, spacing());
}
