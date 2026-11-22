/* The owner's Settings > Performance values, as the page's polls read them.
 *
 * Each read goes to state.config, the last GET /api/config, so a saved change
 * reaches a running poll at its next re-arm without a reload. Until the config
 * has loaded (the first boot polls) the shipped default stands in.
 */
import { state } from '../state.js';

export function perfValue(key, fallback) {
  const perf = state.config && state.config.performance;
  const value = perf && perf.values ? perf.values[key] : undefined;
  return value === undefined || value === null ? fallback : value;
}

// A poll spacing in ms from a seconds knob; a function, so each re-arm asks again.
export function pollMs(key, fallbackMs) {
  return function () { return perfValue(key, fallbackMs / 1000) * 1000; };
}

// Whether a hidden tab stops polling; on unless the owner turned it off.
export function pauseWhenHidden() {
  return perfValue('poll_pause_hidden', true) !== false;
}
