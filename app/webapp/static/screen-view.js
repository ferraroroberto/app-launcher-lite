/* Screen view (ported from app-launcher): the PC's desktop, live,
 * behind the terminal's gate.
 *
 * openScreen() is the one way in; the Settings tab's Screen row calls it. It
 * opens #screenView full screen in the page on every client (lite is
 * Android-only, so upstream's desktop pop-out window is left out).
 *
 * The socket (/api/screen/ws) sends JPEG frames as binary messages and JSON
 * status messages; the view acks each frame once it is drawn, and the server
 * grabs the next only then. The server sends nothing while the screen does
 * not change, so a still picture is a quiet socket, not a stall.
 *
 * Capture runs only while a viewer is connected, so the view closes its
 * socket when it closes and while the page is hidden, and reconnects when it
 * comes back. Pinch-zoom and pan are local (the page itself is zoom-locked):
 * two pointers zoom around their midpoint, one pans while zoomed, the wheel
 * zooms on a desktop.
 */

import { authHeaders, jsonApi, readToken, AuthRequiredError } from './api.js';
import { clearTerminalToken, ensureTerminalToken } from './webauthn.js';

const MAX_ZOOM = 5;
// The widest frame worth asking for: the phone's long side in device pixels,
// capped (the server never sends more than the monitor's own width).
const MAX_FRAME_WIDTH = 1920;
const PRIVATE_ONLY =
  'The screen is private-network-only. Open the launcher over Tailscale or the work VPN.';

const view = {
  open: false,
  monitors: [],
  monitorId: null,
  ws: null,
  frameUrl: '',
  lastFrameAt: 0,
  zoom: { scale: 1, x: 0, y: 0 },
};

function $(id) { return document.getElementById(id); }

// ---------------------------------------------------------------- state

// One status block over the stage: loading / error (a reason, at most one
// Retry) / stale (one muted line over the last frame) / ready (hidden).
// Repeating the same state is a no-op, so a Retry under focus is not rebuilt.
function setState(name, opts) {
  const host = $('screenState');
  if (!host) return;
  const o = opts || {};
  host.dataset.state = name;
  host.hidden = name === 'ready';
  const retry = name === 'error' && o.retry;
  const sig = [name, o.message || '', retry ? 'retry' : ''].join('|');
  if (name === 'ready') {
    delete host.dataset.sig;
    host.replaceChildren();
    return;
  }
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  const msg = document.createElement('p');
  msg.className = 'screen-state-message';
  msg.textContent = o.message || '';
  host.replaceChildren(msg);
  if (retry) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'button-ghost screen-retry';
    btn.textContent = 'Retry';
    btn.addEventListener('click', o.retry);
    host.appendChild(btn);
  }
}

// "Last updated 14:05 · live data unavailable" over the last good frame.
function staleMessage(at) {
  if (!at) return 'Live data unavailable';
  const t = new Date(at);
  const hh = String(t.getHours()).padStart(2, '0');
  const mm = String(t.getMinutes()).padStart(2, '0');
  return 'Last updated ' + hh + ':' + mm + ' · live data unavailable';
}

// ------------------------------------------------------------ entry point

export async function openScreen() {
  const dialog = $('screenView');
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  view.open = true;
  resetZoom();
  await start();
}

async function start() {
  closeSocket();
  setState('loading', { message: 'Connecting to the screen…' });
  let body;
  try {
    const tt = await ensureTerminalToken();
    body = await jsonApi('/api/screen/monitors', { headers: authHeaders({ terminalToken: tt }) });
  } catch (exc) {
    // The login overlay just went up under this top-layer dialog: step
    // aside so it can be used.
    if (exc instanceof AuthRequiredError) { closeView(); return; }
    if (!view.open) return;
    showRefusal(exc && exc.status === 403 ? PRIVATE_ONLY : "Couldn't reach the launcher.");
    return;
  }
  if (!view.open) return;
  view.monitors = (body && body.monitors) || [];
  if (!view.monitors.length) {
    setState('error', { message: 'No display found on the PC.', retry: start });
    renderPicker();
    return;
  }
  if (!view.monitors.some(function (m) { return m.id === view.monitorId; })) {
    const primary = view.monitors.find(function (m) { return m.primary; });
    view.monitorId = (primary || view.monitors[0]).id;
  }
  renderPicker();
  await connect();
}

function showRefusal(message) {
  setState('error', { message: message, retry: start });
}

// --------------------------------------------------------------- picker

// "Main" for the primary, the others by where their centre sits relative to
// the primary's, so a monitor up and to the left (a negative origin) is
// still "Above" when it is more above than left.
export function monitorLabels(monitors) {
  const primary = monitors.find(function (m) { return m.primary; });
  const seen = {};
  return monitors.map(function (m, i) {
    let label;
    if (!primary) {
      label = 'Screen ' + (i + 1);
    } else if (m === primary) {
      label = 'Main';
    } else {
      const dx = (m.left + m.width / 2) - (primary.left + primary.width / 2);
      const dy = (m.top + m.height / 2) - (primary.top + primary.height / 2);
      if (Math.abs(dy) > Math.abs(dx)) label = dy < 0 ? 'Above' : 'Below';
      else label = dx < 0 ? 'Left' : 'Right';
    }
    seen[label] = (seen[label] || 0) + 1;
    return seen[label] > 1 ? label + ' ' + seen[label] : label;
  });
}

function renderPicker() {
  const picker = $('screenPicker');
  if (!picker) return;
  picker.replaceChildren();
  picker.hidden = view.monitors.length < 2;
  if (picker.hidden) return;
  const labels = monitorLabels(view.monitors);
  view.monitors.forEach(function (m, i) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'segmented-item';
    btn.textContent = labels[i];
    btn.dataset.monitor = String(m.id);
    btn.setAttribute('aria-label', labels[i] + ', ' + m.width + ' by ' + m.height);
    btn.setAttribute('aria-pressed', m.id === view.monitorId ? 'true' : 'false');
    btn.addEventListener('click', function () {
      if (m.id === view.monitorId) return;
      view.monitorId = m.id;
      renderPicker();
      clearFrame();
      resetZoom();
      setState('loading', { message: 'Connecting to the screen…' });
      connect();
    });
    picker.appendChild(btn);
  });
}

// --------------------------------------------------------------- socket

function frameWidth() {
  const dpr = window.devicePixelRatio || 1;
  const longSide = Math.max(window.screen.width || 0, window.screen.height || 0);
  return Math.min(MAX_FRAME_WIDTH, Math.round(longSide * dpr) || MAX_FRAME_WIDTH);
}

function socketUrl(terminalToken) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const params = new URLSearchParams();
  params.set('monitor', String(view.monitorId));
  params.set('width', String(frameWidth()));
  const bearer = readToken();
  if (bearer) params.set('token', bearer);
  if (terminalToken) params.set('tt', terminalToken);
  return proto + '//' + location.host + '/api/screen/ws?' + params.toString();
}

async function connect() {
  closeSocket();
  let tt;
  try {
    tt = await ensureTerminalToken();
  } catch (exc) {
    showRefusal('Passkey unlock failed.');
    return;
  }
  if (!view.open) return;
  const ws = new WebSocket(socketUrl(tt));
  ws.binaryType = 'blob';
  view.ws = ws;
  ws.onmessage = function (event) {
    if (ws !== view.ws) return;
    if (typeof event.data === 'string') {
      onStatus(event.data);
      return;
    }
    drawFrame(ws, event.data);
  };
  ws.onclose = function (event) {
    if (ws !== view.ws) return;
    view.ws = null;
    onClose(event);
  };
}

function onStatus(text) {
  let msg = null;
  try { msg = JSON.parse(text); } catch (_) { return; }
  if (!msg || msg.type !== 'status') return;
  if (msg.state === 'unavailable') {
    if (view.frameUrl) {
      setState('stale', { message: staleMessage(view.lastFrameAt) });
    } else {
      setState('error', {
        message: "The PC's screen can't be read right now. It may be locked or asleep.",
      });
    }
  } else if (msg.state === 'live' && !view.frameUrl) {
    setState('loading', { message: 'Connecting to the screen…' });
  }
}

function drawFrame(ws, blob) {
  const img = $('screenImage');
  const url = URL.createObjectURL(blob);
  const ack = function () {
    if (ws === view.ws && ws.readyState === WebSocket.OPEN) ws.send('ack');
  };
  img.onload = function () {
    const previous = view.frameUrl;
    view.frameUrl = url;
    view.lastFrameAt = Date.now();
    if (previous) URL.revokeObjectURL(previous);
    img.hidden = false;
    setState('ready');
    ack();
  };
  img.onerror = function () {
    URL.revokeObjectURL(url);
    ack();
  };
  img.src = url;
}

function onClose(event) {
  if (!view.open) return;
  const reason = event && event.reason ? event.reason : '';
  if (event.code === 4403) {
    showRefusal(PRIVATE_ONLY);
  } else if (event.code === 4401) {
    if (/passkey/i.test(reason)) clearTerminalToken();
    showRefusal(/passkey/i.test(reason) ? 'Passkey unlock required.' : 'Sign in again to view the screen.');
  } else if (event.code === 4404) {
    showRefusal('That display is gone. Retry to pick again.');
  } else {
    // The last frame stays behind the message, so it is still readable.
    setState('error', { message: 'Disconnected from the screen.', retry: start });
  }
}

function closeSocket() {
  const ws = view.ws;
  view.ws = null;
  if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
}

function clearFrame() {
  const img = $('screenImage');
  if (img) {
    img.hidden = true;
    img.removeAttribute('src');
  }
  if (view.frameUrl) URL.revokeObjectURL(view.frameUrl);
  view.frameUrl = '';
  view.lastFrameAt = 0;
}

function closeView() {
  view.open = false;
  closeSocket();
  clearFrame();
  const dialog = $('screenView');
  if (dialog && dialog.open) dialog.close();
}

// ---------------------------------------------------------- zoom and pan

function resetZoom() {
  view.zoom = { scale: 1, x: 0, y: 0 };
  applyZoom();
}

function applyZoom() {
  const img = $('screenImage');
  const stage = $('screenStage');
  if (!img || !stage) return;
  const z = view.zoom;
  const w = stage.clientWidth;
  const h = stage.clientHeight;
  // Keep the zoomed picture covering the stage: no panning past an edge.
  z.x = Math.min(0, Math.max(w - w * z.scale, z.x));
  z.y = Math.min(0, Math.max(h - h * z.scale, z.y));
  img.style.transform = z.scale === 1 ? '' :
    'translate(' + z.x + 'px, ' + z.y + 'px) scale(' + z.scale + ')';
}

// Zoom to `scale` keeping the stage point (qx, qy) where it is.
function zoomAt(scale, qx, qy) {
  const z = view.zoom;
  const next = Math.min(MAX_ZOOM, Math.max(1, scale));
  z.x = qx - (qx - z.x) * (next / z.scale);
  z.y = qy - (qy - z.y) * (next / z.scale);
  z.scale = next;
  applyZoom();
}

function wireZoom(stage) {
  const pointers = new Map();
  let pinch = null;

  function local(e) {
    const r = stage.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  function pinchState() {
    const [a, b] = Array.from(pointers.values());
    return {
      dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
      mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
    };
  }

  stage.addEventListener('pointerdown', function (e) {
    if (e.target.closest('button')) return;
    try { stage.setPointerCapture(e.pointerId); } catch (_) { /* synthetic */ }
    pointers.set(e.pointerId, local(e));
    if (pointers.size === 2) {
      const p = pinchState();
      pinch = { dist: p.dist, mid: p.mid, scale: view.zoom.scale };
    }
  });
  stage.addEventListener('pointermove', function (e) {
    const prev = pointers.get(e.pointerId);
    if (!prev) return;
    const now = local(e);
    pointers.set(e.pointerId, now);
    if (pointers.size === 2 && pinch) {
      const p = pinchState();
      zoomAt(pinch.scale * (p.dist / pinch.dist), p.mid.x, p.mid.y);
    } else if (pointers.size === 1 && view.zoom.scale > 1) {
      view.zoom.x += now.x - prev.x;
      view.zoom.y += now.y - prev.y;
      applyZoom();
    }
  });
  const lift = function (e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
  };
  stage.addEventListener('pointerup', lift);
  stage.addEventListener('pointercancel', lift);
  stage.addEventListener('wheel', function (e) {
    e.preventDefault();
    const q = local(e);
    zoomAt(view.zoom.scale * Math.exp(-e.deltaY * 0.002), q.x, q.y);
  }, { passive: false });
  stage.addEventListener('dblclick', resetZoom);
}

// ----------------------------------------------------------------- wiring

export function wireScreenView() {
  const dialog = $('screenView');
  if (!dialog) return;
  const close = $('screenViewClose');
  if (close) close.addEventListener('click', closeView);
  // Esc closes the dialog natively; stop capture with it.
  dialog.addEventListener('close', function () {
    if (view.open) closeView();
  });
  const stage = $('screenStage');
  if (stage) wireZoom(stage);
  window.addEventListener('resize', applyZoom);
  // A hidden page is not a viewer: drop the socket so capture stops, and
  // pick the stream back up when the page returns.
  document.addEventListener('visibilitychange', function () {
    if (!view.open) return;
    if (document.hidden) closeSocket();
    else if (!view.ws && view.monitorId !== null) connect();
  });
  const row = $('screenOpen');
  if (row) row.addEventListener('click', openScreen);
}
