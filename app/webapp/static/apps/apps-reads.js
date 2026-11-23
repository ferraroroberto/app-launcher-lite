/* The registry and agent reads, without the Code and Apps tabs' drawing.
 *
 * The Board reads state.apps (its issue actions key on the registered
 * projects) and state.agents (session icons), so both reads run at every
 * boot. The code that draws them (apps.js and the Coding list) loads with the
 * Code or Apps tab, which a boot that reopens on the Board does not draw, so
 * these reads live here and that code stays out of the Board's first load.
 */

import { state } from '../state.js';
import { jsonApi, logPollFailure } from '../api.js';

// When the registry last read well: the Apps card's empty states wait for
// one, and a failure after it is a stale line over the last list.
let readAt = 0;

export function appsReadAt() {
  return readAt;
}

// Fills state.apps; the caller draws. A failure leaves the last list in place
// and rejects.
export async function readApps() {
  const body = await jsonApi('/api/apps');
  readAt = Date.now();
  state.apps = body.apps || [];
  state.miniAppsEnabled = !!body.mini_apps_enabled;
}

// Coding-agent detection — which CLIs are installed. Drives the
// enabled/disabled state of the Coding tab's per-tile launch buttons.
// Best-effort: on failure state.agents keeps its conservative fallback.
export async function fetchAgents() {
  try {
    const body = await jsonApi('/api/agents');
    if (Array.isArray(body.agents) && body.agents.length) {
      state.agents = body.agents;
    }
    // Rides on the same payload — the VS Code button isn't an agent,
    // but it's detected the same way. Guarded on the type so an older server
    // that omits the field leaves the conservative fallback in place rather
    // than coercing `undefined` to a hard `false`.
    if (typeof body.vscode_available === 'boolean') {
      state.vscodeAvailable = body.vscode_available;
    }
  } catch (exc) {
    logPollFailure('agents fetch failed', exc);
  }
}
