/* Settings › Speech to text: the switch, the endpoint fields and the
 * connection test behind every record button.
 *
 * Config rides the same round trip as every Settings field (`patchConfig`
 * POSTs, then re-reads GET /api/config). The API key is write-only: the
 * server reports only whether one is set, so its field is always blank and a
 * change replaces the key, while "Remove key" clears it.
 *
 * Recent recordings lists the dictation takes kept on this PC
 * (src/recordings.py), newest first, each with Play and Transcribe; a
 * transcript shows under its take with Copy. The list loads when the sheet
 * opens and on Refresh. Its routes sit behind the same passkey gate as a
 * dictation upload.
 */

import { els } from '../state.js';
import { apiFailToast, apiRaw, copyText, jsonApi, toast } from '../api.js';
import { ensureTerminalToken, terminalJsonApi } from '../webauthn.js';
import { patchConfig, wireConfigSwitch } from './claude-options.js';
import { setSwitch } from '../_vendored/switch/switch.js';

// The connection test's verdicts, strongest first: only `transcribes` proves a
// recording would come back as text.
const _STATE_WORD = {
  transcribes: 'Transcribes',
  refuses_audio: 'Refuses audio',
  reachable: 'Reachable',
  refused: 'Refused',
  unknown: 'Unknown',
};

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
  if (els.speechKeepHours) {
    if (s.keep_recordings_hours_max != null) els.speechKeepHours.max = s.keep_recordings_hours_max;
    setValue(els.speechKeepHours, s.keep_recordings_hours != null ? String(s.keep_recordings_hours) : '');
  }
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

// ------------------------------------------------------ recent recordings
const _RECORDINGS = '/api/transcribe/recordings';
const _STATUS_WORD = {
  recording: 'Recording',
  transcribed: 'Transcribed',
  failed: 'Failed',
  stopped: 'Not transcribed',
  incomplete: 'Incomplete',
};
// Object URLs of takes played since the list was last drawn, freed on redraw.
let playUrls = [];

function lengthText(ms) {
  const total = Math.round((ms || 0) / 1000);
  return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
}

function whenText(created) {
  return new Date(created * 1000).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function setStatus(row, status, error) {
  const word = row.querySelector('.speech-recording-status');
  word.dataset.status = status;
  word.textContent = _STATUS_WORD[status] || status;
  const err = row.querySelector('.speech-recording-error');
  // The server keeps an error only on a failed take (src/recordings.py).
  err.textContent = error || '';
  err.hidden = !err.textContent;
}

async function play(take, row, btn) {
  btn.disabled = true;
  try {
    const res = await apiRaw(_RECORDINGS + '/' + take.id + '/audio', {
      terminalToken: await ensureTerminalToken(),
    });
    if (!res.ok) throw Object.assign(new Error('HTTP ' + res.status), { status: res.status });
    const url = URL.createObjectURL(await res.blob());
    playUrls.push(url);
    let audio = row.querySelector('audio');
    if (!audio) {
      audio = el('audio');
      audio.controls = true;
      row.querySelector('.speech-recording-actions').after(audio);
    }
    audio.src = url;
    audio.play().catch(function () { /* the controls stay for a manual play */ });
  } catch (exc) {
    apiFailToast('Could not play the recording', exc);
  } finally {
    btn.disabled = false;
  }
}

function showTranscript(row, text) {
  let box = row.querySelector('.speech-recording-result');
  if (!box) {
    box = el('div', 'speech-recording-result');
    const p = el('p', 'speech-recording-text');
    const copy = el('button', 'button-surface', 'Copy');
    copy.type = 'button';
    copy.dataset.track = 'speech.recording-copy';
    copy.addEventListener('click', function () { copyText(p.textContent, 'Transcript'); });
    const actions = el('div', 'speech-recording-actions');
    actions.append(copy);
    box.append(p, actions);
    row.append(box);
  }
  box.querySelector('.speech-recording-text').textContent = text || '(nothing heard)';
}

async function transcribe(take, row, btn) {
  btn.disabled = true;
  btn.textContent = 'Transcribing';
  try {
    const body = await terminalJsonApi(_RECORDINGS + '/' + take.id + '/transcribe', { method: 'POST' });
    setStatus(row, 'transcribed', '');
    showTranscript(row, (body && body.transcript) || '');
  } catch (exc) {
    setStatus(row, 'failed', (exc && exc.message) || '');
    apiFailToast('Transcription failed', exc);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Transcribe';
  }
}

function rowFor(take) {
  const row = el('li', 'speech-recording');
  row.dataset.recordingId = take.id;
  const head = el('div', 'speech-recording-head');
  head.append(
    el('span', 'speech-recording-when', whenText(take.created)),
    el('span', 'speech-recording-meta', lengthText(take.ms)),
    el('span', 'speech-recording-status'),
  );
  const error = el('p', 'field-help speech-recording-error');
  const actions = el('div', 'speech-recording-actions');
  const playBtn = el('button', 'button-surface', 'Play');
  playBtn.type = 'button';
  playBtn.dataset.track = 'speech.recording-play';
  playBtn.addEventListener('click', function () { play(take, row, playBtn); });
  const txBtn = el('button', 'button-tint', 'Transcribe');
  txBtn.type = 'button';
  txBtn.dataset.track = 'speech.recording-transcribe';
  txBtn.addEventListener('click', function () { transcribe(take, row, txBtn); });
  // A take whose first chunk never landed has no audio to play or send.
  playBtn.disabled = txBtn.disabled = !take.bytes;
  actions.append(playBtn, txBtn);
  row.append(head, error, actions);
  setStatus(row, take.status, take.error);
  return row;
}

async function loadRecordings() {
  const list = els.speechRecordings;
  const note = els.speechRecordingsState;
  if (!list || !note) return;
  try {
    const body = await terminalJsonApi(_RECORDINGS);
    const takes = (body && body.recordings) || [];
    playUrls.forEach(function (u) { URL.revokeObjectURL(u); });
    playUrls = [];
    list.replaceChildren(...takes.map(rowFor));
    note.textContent = 'No recordings kept.';
    note.hidden = takes.length > 0;
  } catch (exc) {
    note.textContent = 'Could not read the recordings.';
    note.hidden = false;
    apiFailToast('Recordings', exc);
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
  if (els.speechRecordingsRefresh) els.speechRecordingsRefresh.addEventListener('click', loadRecordings);
  // The list is read each time the sheet opens, however it was opened.
  const sheet = document.getElementById('speechSheet');
  if (sheet) {
    new MutationObserver(function () {
      if (sheet.open) loadRecordings();
    }).observe(sheet, { attributes: true, attributeFilter: ['open'] });
  }
}
