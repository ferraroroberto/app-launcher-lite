/* Settings > Appearance > Full screen.
 *
 * A browser tab shows its address bar and toolbar, which take room on a phone
 * and cannot be hidden by the page. Where the browser offers the Fullscreen
 * API this switch hides them. The choice is kept per device in localStorage;
 * a browser only grants full screen to a user gesture, so after a reload the
 * switch stays on and the first tap anywhere puts the page back in full
 * screen. Leaving it any other way (the back gesture, Esc, the system UI)
 * is a deliberate exit and turns the switch off, so the next reload does not
 * pull the person back in.
 *
 * Exit is this same switch, or the back gesture: Settings is one tap away
 * from every page header, so no extra control sits over the content.
 */
import { els } from '../state.js';
import { setSwitch } from '../_vendored/switch/switch.js';

const KEY = 'launcher.fullscreen';
const ROOT = document.documentElement;

/* The standard API where it exists, else the WebKit-prefixed one (older
 * Safari and Chrome builds). Null means this browser has no full screen for
 * web pages, which the row says instead of pretending the switch works. */
function fsApi() {
  if (document.fullscreenEnabled && ROOT.requestFullscreen) {
    return {
      element: function () { return document.fullscreenElement; },
      request: function () { return ROOT.requestFullscreen({ navigationUI: 'hide' }); },
      exit: function () { return document.exitFullscreen(); },
      event: 'fullscreenchange',
    };
  }
  if (document.webkitFullscreenEnabled && ROOT.webkitRequestFullscreen) {
    return {
      element: function () { return document.webkitFullscreenElement; },
      request: function () { return ROOT.webkitRequestFullscreen(); },
      exit: function () { return document.webkitExitFullscreen(); },
      event: 'webkitfullscreenchange',
    };
  }
  return null;
}

function wanted() {
  try { return localStorage.getItem(KEY) === '1'; } catch (_) { return false; }
}

function setWanted(on) {
  try {
    if (on) localStorage.setItem(KEY, '1');
    else localStorage.removeItem(KEY);
  } catch (_) { /* storage blocked: full screen still works for this visit */ }
}

function showStatus(text) {
  const status = els.fullscreenStatus;
  if (!status) return;
  status.textContent = text || '';
  status.hidden = !text;
}

export function renderFullscreen() {
  const btn = els.fullscreenToggle;
  if (!btn) return;
  const api = fsApi();
  if (!api) {
    setSwitch(btn, false);
    btn.disabled = true;
    showStatus("This browser doesn't offer full screen for web pages.");
    return;
  }
  btn.disabled = false;
  const on = wanted();
  setSwitch(btn, on);
  const inFullscreen = !!api.element();
  showStatus(on && !inFullscreen ? 'On for this device. Tap anywhere to go full screen.' : '');
}

async function enter(api) {
  try {
    await api.request();
  } catch (_) {
    // No user gesture, or the browser refused: the switch stays on and the
    // next tap tries again.
    armReentry(api);
  }
  renderFullscreen();
}

/* One capture-phase tap re-enters full screen after a reload. The tap still
 * reaches whatever it landed on. A tap on the switch itself is left to the
 * switch, which would otherwise be undone in the same gesture. */
function armReentry(api) {
  if (!wanted() || api.element()) return;
  document.addEventListener('click', function (event) {
    const btn = els.fullscreenToggle;
    if (btn && btn.contains(event.target)) return;
    if (wanted() && !api.element()) enter(api);
  }, { capture: true, once: true });
}

export function wireFullscreen() {
  const btn = els.fullscreenToggle;
  const api = fsApi();
  renderFullscreen();
  if (!btn || !api) return;

  let wasFullscreen = !!api.element();
  document.addEventListener(api.event, function () {
    const now = !!api.element();
    // Leaving by the back gesture or Esc is a choice: forget it.
    if (wasFullscreen && !now) setWanted(false);
    wasFullscreen = now;
    renderFullscreen();
  });

  btn.addEventListener('click', function () {
    if (wanted()) {
      setWanted(false);
      if (api.element()) Promise.resolve(api.exit()).catch(function () { /* already out */ });
      renderFullscreen();
    } else {
      setWanted(true);
      enter(api);
    }
  });

  armReentry(api);
}
