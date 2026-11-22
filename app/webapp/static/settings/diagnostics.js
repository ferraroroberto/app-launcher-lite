/* Settings > Diagnostics.
 *
 * Save diagnostics asks the PC to write the chosen window of every Lite log
 * to one redacted text file (POST /api/diagnostics) and shows where it
 * landed. Download fetches that file back (GET /api/diagnostics/<name>) and
 * hands it to the browser, so the phone can keep or send it. Both calls carry
 * the passkey terminal token: the file holds terminal-grade content.
 */
import { els } from '../state.js';
import { apiFailToast, apiRaw, toast } from '../api.js';
import { ensureTerminalToken, terminalJsonApi } from '../webauthn.js';
import { saveAs } from '../shared/save-as.js';

let savedName = '';

function showResult(text) {
  if (!els.diagnosticsResult) return;
  els.diagnosticsResult.textContent = text || '';
  els.diagnosticsResult.hidden = !text;
}

function kilobytes(bytes) {
  return Math.max(1, Math.round((bytes || 0) / 1024)) + ' KB';
}

async function save() {
  const btn = els.diagnosticsSave;
  const minutes = Number(els.diagnosticsWindow ? els.diagnosticsWindow.value : 60);
  btn.disabled = true;
  showResult('Saving…');
  try {
    const body = await terminalJsonApi('/api/diagnostics', { method: 'POST', body: { minutes: minutes } });
    savedName = body.name;
    showResult('Saved ' + body.records + ' log records (' + kilobytes(body.bytes) + ') to ' + body.path);
    els.diagnosticsDownload.hidden = false;
    toast('Diagnostics saved.', 'good');
  } catch (exc) {
    showResult('');
    apiFailToast('Save diagnostics failed', exc);
  } finally {
    btn.disabled = false;
  }
}

async function download() {
  if (!savedName) return;
  try {
    const res = await apiRaw('/api/diagnostics/' + encodeURIComponent(savedName), {
      terminalToken: await ensureTerminalToken(),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    saveAs(savedName, await res.blob());
  } catch (exc) {
    apiFailToast('Download failed', exc);
  }
}

export function wireDiagnostics() {
  if (els.diagnosticsSave) els.diagnosticsSave.addEventListener('click', save);
  if (els.diagnosticsDownload) els.diagnosticsDownload.addEventListener('click', download);
}
