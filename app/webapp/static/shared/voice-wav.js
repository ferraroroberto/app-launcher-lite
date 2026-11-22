/* A dictation take as 16 kHz mono 16-bit PCM WAV: the one audio shape every
 * OpenAI-shaped speech backend decodes. A browser records MP4 or WebM, which
 * stricter backends refuse, so the finished take is decoded with Web Audio,
 * resampled and downmixed by an OfflineAudioContext, and written as WAV here.
 */

const WAV_RATE = 16000;

// The WAV encoder: a 44-byte RIFF header, then little-endian 16-bit samples.
function encodeWav(samples, rate) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = function (at, s) {
    for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i));
  };
  text(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, 'WAVE');
  text(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  text(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Blob([view.buffer], { type: 'audio/wav' });
}

// The recorded blob as a portable WAV, or null when this browser has no Web
// Audio or cannot decode its own recording; the caller then uploads the
// original and the server converts it if it can.
export async function takeToWav(blob) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!Ctx || !Offline) return null;
  try {
    const bytes = await blob.arrayBuffer();
    const ctx = new Ctx();
    let decoded;
    try {
      decoded = await ctx.decodeAudioData(bytes);
    } finally {
      ctx.close();
    }
    const frames = Math.max(1, Math.ceil(decoded.duration * WAV_RATE));
    // A one-channel destination downmixes the source; its rate resamples it.
    const offline = new Offline(1, frames, WAV_RATE);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const rendered = await offline.startRendering();
    return encodeWav(rendered.getChannelData(0), WAV_RATE);
  } catch (exc) {
    console.warn('[dictation] Web Audio could not convert the take to WAV', exc);
    return null;
  }
}
