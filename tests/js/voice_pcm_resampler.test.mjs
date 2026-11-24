// Unit pin for the dictation PCM capture's resampler (shared/voice-pcm.js):
// a 48 kHz or 44.1 kHz microphone becomes 16 kHz samples, with the right
// count, a steady level preserved and no drift across batches. Runs under
// plain Node; invoked by tests/unit/repo/test_voice_pcm_resampler.py.

import assert from 'node:assert/strict';

const { createResampler } = await import('../../app/webapp/static/shared/voice-pcm.js');

function resample(inRate, input, batch) {
  const rs = createResampler(inRate, 16000);
  const out = [];
  for (let i = 0; i < input.length; i += batch) out.push(...rs.push(input.slice(i, i + batch)));
  out.push(...rs.flush());
  return out;
}

for (const rate of [48000, 44100]) {
  const one = new Float32Array(rate).fill(0.5); // one second of a constant level
  const out = resample(rate, one, 1024);
  assert.ok(Math.abs(out.length - 16000) <= 1, `${rate} Hz: ${out.length} samples for one second`);
  assert.ok(out.every((v) => Math.abs(v - 0.5) < 1e-6), `${rate} Hz: the level changed`);
  // The same samples in odd-sized batches resample identically.
  const odd = resample(rate, one, 333);
  assert.equal(odd.length, out.length, `${rate} Hz: batching changed the length`);
}

// A tone above the new Nyquist (12 kHz at 48 kHz) is averaged away, not aliased loud.
const hiss = Float32Array.from({ length: 48000 }, (_, i) => Math.sin(2 * Math.PI * 12000 * i / 48000));
const peak = Math.max(...resample(48000, hiss, 2048).map(Math.abs));
assert.ok(peak < 0.5, `a 12 kHz tone survived the downsample at ${peak}`);

// Nothing in, nothing out.
assert.deepEqual(resample(48000, new Float32Array(0), 1024), []);
console.log('voice-pcm resampler: ok');
