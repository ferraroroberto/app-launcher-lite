/* Usage log wiring: which views and controls the owner uses, on which device
 * class (contract: _vendored/usage-log/README.md).
 *
 * The loopback PC bypasses the bearer token, a phone on the tunnel or tailnet does
 * not, so the client's one endpoint needs the token or the phone's events would 401
 * silently and the log would only describe the desktop. The vendored client takes it
 * through its `headers` option, a function so a rotated token is read on every send;
 * nothing here replaces window.fetch or navigator.sendBeacon.
 */

import { initUsageLog } from './_vendored/usage-log/usage-log.js';
import { authHeaders } from './api.js';

let usage = null;

// Start the logger once, before the tabs are wired, so the restored tab counts as
// the first view.
export function wireUsageLog() {
  usage = initUsageLog({ headers: function () { return authHeaders(); } });
}

// tabs.js announces every tab switch; the tab name is the view id.
export function trackView(id) {
  // dynamic-id-exempt: the ids are the constant data-tab values in index.html
  if (usage) usage.trackView(id);
}
