/* Settings > Appearance > Install as app.
 *
 * Says whether this browser can install Lite as an app and, when it cannot,
 * why, so the answer is never a silent no. A page cannot read whether its
 * certificate is trusted, so the one thing it observes is the browser's own
 * `beforeinstallprompt`: a browser fires it only for an installable page
 * (secure origin with a trusted certificate, a manifest). Fired means the
 * browser will install it; not fired on a secure page is reported as "not
 * offered" with the usual reasons, never as a certain cause.
 *
 * The listener attaches when this module loads, before the page's load event
 * and so before any browser can fire the event.
 */
import { els } from '../state.js';

let offered = false;
let installed = false;

window.addEventListener('beforeinstallprompt', function () {
  offered = true;
  renderInstallReadiness();
});
window.addEventListener('appinstalled', function () {
  installed = true;
  renderInstallReadiness();
});

function runsAsApp() {
  try {
    if (navigator.standalone === true) return true;
    return ['standalone', 'fullscreen', 'minimal-ui'].some(function (mode) {
      return window.matchMedia('(display-mode: ' + mode + ')').matches;
    });
  } catch (_) {
    return false;
  }
}

/* One state, a short label and one sentence, from what the page can observe. Exported for
 * the e2e that pins each branch. */
export function installReadiness() {
  if (installed || runsAsApp()) {
    return { state: 'installed', label: 'Installed', text: 'Installed: Lite is running as an app.' };
  }
  if (!window.isSecureContext) {
    return {
      state: 'blocked',
      label: 'Blocked',
      text: "Can't install: this page isn't on a secure (HTTPS) address.",
    };
  }
  if (offered) {
    return {
      state: 'offered',
      label: 'Ready',
      text: 'Ready: use the browser menu, Install app, to add Lite.',
    };
  }
  if (!('onbeforeinstallprompt' in window)) {
    return {
      state: 'unsupported',
      label: 'Unavailable',
      text: "This browser has no install prompt for web apps. Look for Add to Home screen in its menu.",
    };
  }
  return {
    state: 'not-offered',
    label: 'Not offered',
    text: "Not offered. Usually the certificate isn't trusted on this device, so the browser marks the page not secure and won't install it. A page can't tell which.",
  };
}

export function renderInstallReadiness() {
  const row = els.installReadinessRow;
  const value = els.installReadinessValue;
  const help = els.installReadinessStatus;
  if (!row || !value || !help) return;
  const result = installReadiness();
  row.dataset.installState = result.state;
  value.textContent = result.label;
  help.textContent = result.text;
}
