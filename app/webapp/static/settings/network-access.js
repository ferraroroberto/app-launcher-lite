/* Settings › Terminal › the two private-network switches.
 *
 * "Allow private network without Tailscale" trusts every private address on
 * the terminal-grade routes (transcript, terminal, input, image, passkey
 * ceremony); "Also allow screen view without Tailscale" does the same for the
 * screen view, which shows the whole desktop, so it has its own switch that
 * can only be on while the first is. Turning either ON asks first, naming
 * what it opens. Both save through the passkey-gated config write, because a
 * switch that widens access must not be reachable through the widening; the
 * server takes effect at once, no restart.
 */

import { els } from '../state.js';
import { apiFailToast, toast } from '../api.js';
import { confirmDialog } from '../confirm-dialog.js';
import { setSwitch } from '../_vendored/switch/switch.js';
import { terminalJsonApi } from '../webauthn.js';

const PRIVATE_RANGES = '10/8, 172.16/12, 192.168/16, link-local and IPv6 unique-local';

const WARNING = {
  terminal_allow_private_lan: {
    title: 'Allow the private network?',
    message: 'This trusts every private address (' + PRIVATE_RANGES + ') for transcripts, '
      + 'the terminal and the other session views. The token and passkey still apply, '
      + 'but anything else on that network can reach the login. '
      + 'The narrower option is tailnet_allowlist with one subnet.',
    action: 'Allow private network',
  },
  screen_allow_private_lan: {
    title: 'Allow screen view on the private network?',
    message: 'The screen view shows this whole PC, terminals included. Any private address '
      + 'that passes the token and passkey can then watch it.',
    action: 'Allow screen view',
  },
};

// Paints both switches from GET /api/config. The screen switch stands down
// while the first is off: the server refuses it then, and clears it when the
// first goes off.
export function renderNetworkAccess(body) {
  const terminalOn = body.terminal_allow_private_lan === true;
  if (els.privateLanToggle) setSwitch(els.privateLanToggle, terminalOn);
  if (els.screenPrivateLanToggle) {
    setSwitch(els.screenPrivateLanToggle, terminalOn && body.screen_allow_private_lan === true);
    els.screenPrivateLanToggle.disabled = !terminalOn;
  }
}

function wireSwitch(el, key, refresh) {
  if (!el) return;
  el.addEventListener('click', async function () {
    const next = el.getAttribute('aria-checked') !== 'true';
    if (next) {
      const w = WARNING[key];
      const ok = await confirmDialog({ title: w.title, message: w.message, action: w.action });
      if (!ok) return;
    }
    setSwitch(el, next);
    try {
      await terminalJsonApi('/api/config', { method: 'POST', body: { [key]: next } });
    } catch (exc) {
      apiFailToast('Save failed', exc);
      setSwitch(el, !next);
      return;
    }
    await refresh().catch(function () {});
    toast((key === 'screen_allow_private_lan' ? 'Screen view' : 'Private network')
      + (next ? ' on.' : ' off.'), 'good');
  });
}

// `refresh` re-reads GET /api/config (claude-options' fetchConfig), passed in
// so this file does not import the module that renders it.
export function wireNetworkAccess(refresh) {
  wireSwitch(els.privateLanToggle, 'terminal_allow_private_lan', refresh);
  wireSwitch(els.screenPrivateLanToggle, 'screen_allow_private_lan', refresh);
}
