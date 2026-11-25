/* Settings › Local hub: the address the PC load sheet reads machine health from.
 *
 * The server checks the address answers /healthz before it saves anything, so a
 * refusal arrives as the reason in words and the saved value stays as it was.
 * Empty is a valid save: it turns the hub off. The field follows the config
 * (renderHub, run on every config read); the status line below it shows the
 * last attempt only, so a stale "Saved" never outlives the next edit.
 */

import { state } from '../state.js';
import { jsonApi } from '../api.js';
import { fetchConfig } from './claude-options.js';

const byId = function (id) { return document.getElementById(id); };

// Puts the saved address in the field (unless it is being typed in) and returns
// what the Settings row shows: the saved host, or Off.
export function renderHub(cfg) {
  const url = (cfg && cfg.llm_hub_url) || '';
  const field = byId('hubUrl');
  if (field && field !== document.activeElement) field.value = url;
  if (!url) return 'Off';
  try { return new URL(url).host; } catch (_) { return url; }
}

function showResult(text, tone) {
  const out = byId('hubResult');
  out.textContent = text;
  out.className = tone === 'error' ? 'field-error' : 'field-help';
  out.hidden = !text;
}

// Saves `url` ('' clears it) and says how it went; a refusal is the server's own
// words (bad address, nothing listening, no answer) and leaves the saved value as
// it was. The controls stand down while the hub is being asked.
async function saveUrl(url) {
  const field = byId('hubUrl');
  const useLocal = byId('hubUseLocal');
  field.disabled = useLocal.disabled = true;
  showResult(url ? 'Checking the hub…' : '', 'ok');
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ llm_hub_url: url }),
    });
    await fetchConfig();
    showResult(url ? 'Saved. The hub answered at ' + url + '.' : 'Saved. No hub is set up.', 'ok');
  } catch (exc) {
    showResult((exc && exc.message) || 'Not saved.', 'error');
  } finally {
    field.disabled = useLocal.disabled = false;
  }
}

export function wireHub() {
  const field = byId('hubUrl');
  // `change` fires on leaving the field or Enter, never per keystroke, so a
  // half-typed address is never sent to the hub.
  field.addEventListener('change', function () { saveUrl(field.value.trim()); });
  byId('hubUseLocal').addEventListener('click', function () {
    const local = (state.config && state.config.llm_hub_local_url) || '';
    field.value = local;
    saveUrl(local);
  });
}
