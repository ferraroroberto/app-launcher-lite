/* Settings › Connections › Serve over: HTTPS, HTTP or both, and the
 * certificate HTTPS needs.
 *
 * The choice saves through `patchConfig` like every Settings field and
 * applies when the app restarts, so the sheet also says what this page is on
 * now. "Create certificate" shows only when no pair exists: the server never
 * replaces one (a 409), whoever made it.
 */

import { els } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { fetchConfig, patchConfig } from './claude-options.js';

const MODE_WORD = { auto: 'Automatic', https: 'HTTPS', http: 'HTTP', both: 'Both' };

function modeButtons() {
  return document.querySelectorAll('#servingMode [data-mode]');
}

// What the Settings row shows: the saved choice.
export function servingValue(s) {
  if (!s) return '';
  const word = MODE_WORD[s.mode] || s.mode;
  return s.mode === 'both' ? word + ' · HTTP ' + s.http_port : word;
}

// What the last start did with the second port; empty unless it failed.
function bindText(s) {
  const b = s.http_bind;
  if (!b || b.bound !== false) return '';
  return 'The last start could not use port ' + b.port + ': another program is using it, so plain HTTP is off and HTTPS is still served. Pick another port and restart the app.';
}

function certText(cert) {
  if (!cert || !cert.present) return 'No certificate on this PC.';
  const kind = cert.self_signed === true ? 'A self-signed certificate'
    : cert.self_signed === false ? 'A certificate' : 'A certificate that could not be read';
  return kind + ' is in place' + (cert.expires ? ', valid until ' + cert.expires + '.' : '.');
}

export function renderServing(cfg) {
  const s = cfg && cfg.serving;
  if (!s) return;
  modeButtons().forEach(function (btn) {
    btn.setAttribute('aria-pressed', String(btn.dataset.mode === s.mode));
  });
  if (els.servingHttpPort && els.servingHttpPort !== document.activeElement) {
    els.servingHttpPort.value = String(s.http_port);
  }
  if (els.servingHttpPortRow) els.servingHttpPortRow.hidden = s.mode !== 'both';
  if (els.servingHttpPortHelp) els.servingHttpPortHelp.hidden = s.mode !== 'both';
  if (els.servingNow) {
    els.servingNow.textContent = window.isSecureContext
      ? 'This page is on a secure address: recording can work here.'
      : 'This page is on plain HTTP: recording needs HTTPS.';
  }
  if (els.servingBind) {
    els.servingBind.textContent = bindText(s);
    els.servingBind.hidden = !els.servingBind.textContent;
  }
  if (els.servingCert) els.servingCert.textContent = certText(s.cert);
  if (els.servingCreateCert) els.servingCreateCert.hidden = !!(s.cert && s.cert.present);
}

async function createCertificate() {
  const btn = els.servingCreateCert;
  btn.disabled = true;
  try {
    await jsonApi('/api/settings/certificate', { method: 'POST' });
    await fetchConfig();
    toast('Certificate created. It applies when the app restarts.', 'good');
  } catch (exc) {
    apiFailToast('Certificate not created', exc);
  } finally {
    btn.disabled = false;
  }
}

export function wireServing() {
  modeButtons().forEach(function (btn) {
    btn.addEventListener('click', async function () {
      if (btn.getAttribute('aria-pressed') === 'true') return;
      if (await patchConfig({ serve_over: btn.dataset.mode })) {
        toast('Serve over ' + MODE_WORD[btn.dataset.mode] + ' saved. It applies when the app restarts.', 'good');
      }
    });
  });
  if (els.servingHttpPort) {
    els.servingHttpPort.addEventListener('change', async function () {
      const port = Number(els.servingHttpPort.value);
      if (await patchConfig({ serve_http_port: port })) toast('HTTP port saved.', 'good');
    });
  }
  if (els.servingCreateCert) els.servingCreateCert.addEventListener('click', createCertificate);
}
