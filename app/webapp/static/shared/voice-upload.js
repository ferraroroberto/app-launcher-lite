/* The dictation take as the form body /api/transcribe receives. */

// The PCM capture's WAV, or null when no capture ran or it heard nothing.
async function pcmWav(pcmStart) {
  if (!pcmStart) return null;
  try {
    const capture = await pcmStart;
    return capture ? await capture.stop() : null;
  } catch (exc) {
    console.info('[dictation] PCM capture did not finish', (exc && exc.message) || exc);
    return null;
  }
}

// The take as the upload's form body, with the path that built it: `pcm`, the
// 16 kHz mono WAV recorded straight from the microphone (no container is ever
// decoded, so it works on iPhone Safari); `decoded`, the same WAV converted
// from the recorder's blob with Web Audio; or `raw`, the browser's own
// recording, which the server converts when it has ffmpeg. `step` names the
// browser step that failed on the way to `raw`, for the failure toast. The
// converter loads on the first take, so it adds nothing to page boot, and a
// take it cannot convert tells the kept copy why.
export async function uploadForm(blob, keep, pcmStart) {
  const fd = new FormData();
  let wav = await pcmWav(pcmStart);
  let path = 'pcm';
  let step = '';
  if (!wav) {
    path = 'decoded';
    try {
      const { takeToWav } = await import('./voice-wav.js');
      wav = await takeToWav(blob, function (reason, why) {
        step = why || '';
        if (keep) keep.note(reason);
      });
    } catch (exc) {
      console.warn('[dictation] the WAV converter did not load', exc);
      step = 'loading the WAV converter';
      if (keep) keep.note('the WAV converter did not load');
    }
  }
  if (wav) {
    console.info('[dictation] uploading the take as WAV via ' + path);
    fd.append('file', wav, 'recording.wav');
    return { fd: fd, path: path, step: '' };
  }
  const ext = (blob.type && blob.type.indexOf('mp4') >= 0) ? 'mp4' : 'webm';
  console.info('[dictation] uploading the browser recording as ' + ext + ' (raw): it could not be converted to WAV here');
  fd.append('file', blob, 'recording.' + ext);
  return { fd: fd, path: 'raw', step: step };
}
