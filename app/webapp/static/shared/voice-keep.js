/* Keep each dictation take on disk while it is recorded (src/recordings.py).
 *
 * A second MediaRecorder on the take's own stream sends a chunk every few
 * seconds to /api/transcribe/recordings, so a crashed or reloaded page, or a
 * failed transcription, still leaves the audio on the PC for Settings > Speech
 * to text to replay and transcribe again. The recorder the composer
 * transcribes is untouched: it records and uploads exactly as before.
 *
 * Keeping a copy must never delay or break a dictation: every request here is
 * fire-and-forget, nothing the dictation awaits, and every failure is logged
 * and dropped.
 */

import { apiRaw, apiRawJson } from '../api.js';
import { readTerminalToken } from '../webauthn.js';

const _BASE = '/api/transcribe/recordings';
// How much audio a crash can cost: the slice still in the recorder.
const _SLICE_MS = 3000;

function post(path, body) {
  return apiRawJson(path, {
    method: 'POST', terminalToken: readTerminalToken(),
    contentType: 'application/json', body: JSON.stringify(body),
  });
}

function quiet(what) {
  return function (exc) {
    console.info('[dictation] keeping this take: ' + what + ' failed', (exc && exc.message) || exc);
    return null;
  };
}

// Start keeping a take recorded from `stream` in `mime`. Returns
// `{ stop, note(reason), end(status, error) }`, or null when this browser cannot run a
// second recorder; `end` stops the copy and records how the take ended
// (stopped, transcribed or failed) once every chunk has landed.
export function keepTake(stream, mime) {
  let recorder;
  try {
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    recorder.start(_SLICE_MS);
  } catch (exc) {
    quiet('the second recorder')(exc);
    return null;
  }
  const t0 = Date.now();
  const stopped = new Promise(function (resolve) { recorder.addEventListener('stop', resolve); });
  let lost = false;
  let note = '';
  // Every request rides one chain, so chunks land in order and the outcome last.
  let chain = post(_BASE, { mime: recorder.mimeType || mime || 'audio/webm' })
    .then(function (body) { return (body && body.id) || null; }, quiet('opening the recording'));
  recorder.addEventListener('dataavailable', function (ev) {
    if (!ev.data || !ev.data.size) return;
    const data = ev.data;
    const ms = Date.now() - t0;
    chain = chain.then(function (id) {
      if (!id || lost) return id;
      return apiRaw(_BASE + '/' + id + '/chunk?ms=' + ms, {
        method: 'POST', terminalToken: readTerminalToken(),
        contentType: 'application/octet-stream', body: data,
      }).then(function (res) {
        if (!res.ok) { lost = true; console.info('[dictation] keeping this take stopped: HTTP ' + res.status); }
        return id;
      }, function (exc) { lost = true; quiet('a chunk upload')(exc); return id; });
    });
  });
  function stop() {
    if (recorder.state !== 'inactive') {
      try { recorder.stop(); } catch (_) { /* stop fires anyway */ }
    }
  }
  return {
    stop: stop,
    // Why the browser could not convert this take to WAV; sent with its outcome.
    note: function (reason) { note = reason || ''; },
    end: function (status, error) {
      stop();
      // The recorder's last chunk is queued before 'stop' fires, so the chain
      // read after it holds every chunk.
      stopped.then(function () { return chain; }).then(function (id) {
        if (!id) return null;
        return post(_BASE + '/' + id + '/finish', { status: status, error: error || '', note: note });
      }).catch(quiet('recording the outcome'));
    },
  };
}
