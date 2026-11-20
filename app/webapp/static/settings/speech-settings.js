/* Settings › Speech to text: the switch, the endpoint fields and the
 * connection test behind every record button.
 *
 * Config rides the same round trip as every Settings field (`patchConfig`
 * POSTs, then re-reads GET /api/config). The API key is write-only: the
 * server reports only whether one is set, so its field is always blank and a
 * change replaces the key, while "Remove key" clears it.
 */

import { els } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { patchConfig, wireConfigSwitch } from './claude-options.js';
import { setSwitch } from '../_vendored/switch/switch.js';

const _STATE_WORD = { reachable: 'Reachable', refused: 'Refused', unknown: 'Unknown' };

// Saves still in flight, so the connection test never probes with a
// half-saved endpoint: a field's `change` fires on blur, just before the tap.
let pending = Promise.resolve();

function save(patch, message) {
  pending = pending.then(async function () {
    if (await patchConfig(patch) && message) toast(message, 'good');
  });
  return pending;
}

function setValue(el, value) {
  if (el && el !== document.activeElement) el.value = value;
}

export function renderSpeech(cfg) {
  const s = (cfg && cfg.speech) || {};
  if (els.speechEnabledToggle) setSwitch(els.speechEnabledToggle, s.enabled === true);
  setValue(els.speechBaseUrl, s.base_url || '');
  setValue(els.speechModel, s.model || '');
  if (els.speechApiKeyState) {
    els.speechApiKeyState.textContent = s.api_key_set ? 'A key is set.' : 'No key set.';
  }
  if (els.speechApiKeyClear) els.speechApiKeyClear.hidden = !s.api_key_set;
}

// What the Settings row shows: the state of dictation at a glance.
export function speechValue(s) {
  if (!s || !s.enabled) return 'Off';
  return s.base_url ? 'On' : 'No URL';
}

function showResult(state, detail) {
  const out = els.speechTestResult;
  out.textContent = _STATE_WORD[state] + ': ' + detail;
  out.dataset.state = state;
  out.hidden = false;
}

async function testConnection() {
  const btn = els.speechTest;
  btn.disabled = true;
  els.speechTestResult.hidden = true;
  try {
    await pending;
    const body = await jsonApi('/api/speech/test', { method: 'POST' });
    showResult(_STATE_WORD[body.state] ? body.state : 'unknown', body.detail || '');
  } catch (exc) {
    // A refusal to even run the probe (no base URL) is the server's own text.
    apiFailToast('Test failed', exc);
    showResult('unknown', (exc && exc.message) || 'The test could not run.');
  } finally {
    btn.disabled = false;
  }
}

export function wireSpeech() {
  wireConfigSwitch(els.speechEnabledToggle, 'speech_enabled', { label: 'Dictation' });
  [
    [els.speechBaseUrl, 'speech_base_url', 'Speech base URL saved.'],
    [els.speechModel, 'speech_model', 'Speech model saved.'],
  ].forEach(function (f) {
    if (!f[0]) return;
    f[0].addEventListener('change', function () {
      save({ [f[1]]: f[0].value.trim() }, f[2]);
    });
  });
  if (els.speechApiKey) {
    els.speechApiKey.addEventListener('change', function () {
      const key = els.speechApiKey.value.trim();
      if (!key) return;
      els.speechApiKey.value = '';
      save({ speech_api_key: key }, 'Speech API key saved.');
    });
  }
  if (els.speechApiKeyClear) {
    els.speechApiKeyClear.addEventListener('click', function () {
      save({ speech_api_key: '' }, 'Speech API key removed.');
    });
  }
  if (els.speechTest) els.speechTest.addEventListener('click', testConnection);
}
