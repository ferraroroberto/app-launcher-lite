"""Dictation reaches a speech backend that decodes only 16 kHz mono WAV.

Chromium's fake microphone (`--use-fake-device-for-media-stream`) feeds the real
MediaRecorder and Web Audio path of `voice.js`; the take goes through the
disposable webapp's `/api/transcribe` to `tests/fake_speech_backend.py`, which
answers an opaque HTTP 500 to anything but that WAV. The WAV is recorded as PCM
from the microphone stream, so no recorder container is decoded; with PCM
capture removed the page converts the recorder's blob, and with Web Audio
removed too it uploads the recorder's own container: the server then converts
it (ffmpeg on PATH) or says the format is unsupported, never a bare HTTP 500,
and the toast names the browser step that failed. On a page that is not a
secure origin a tap says HTTPS is needed instead of recording.
"""

from __future__ import annotations

import struct

import pytest
from playwright.sync_api import Page, expect

from src.clients.speech_audio import find_ffmpeg
from tests import fake_speech_backend
from tests.e2e.fixtures.waits import wait_until
from tests.e2e.settings._speech import set_speech

# Chromium only: WebKit has no fake microphone.
pytestmark = [pytest.mark.smoke, pytest.mark.unbundled]

FIELD = "#e2eDictationText"

# Mounts one dictation on a throwaway button and textarea, importing voice.js
# from the URL the app itself loaded so the module and its state are shared.
_MOUNT = """async () => {
  const hit = performance.getEntriesByType('resource').find(e => /\\/voice\\.js/.test(e.name));
  const mod = await import(hit ? hit.name : '/static/voice.js');
  const button = document.createElement('button');
  const field = document.createElement('textarea');
  field.id = 'e2eDictationText';
  document.body.append(button, field);
  window.__dictation = mod.createDictation({ button, getTextarea: () => field });
}"""


# True once a kept take with audio in it reads transcribed (shared/voice-keep.js).
_KEPT_TRANSCRIBED = """async () => {
  const hit = performance.getEntriesByType('resource').find(e => /\\/api\\.js/.test(e.name));
  const api = await import(hit ? hit.name : '/static/api.js');
  const body = await api.jsonApi('/api/transcribe/recordings');
  return body.recordings.some(r => r.status === 'transcribed' && r.bytes > 0);
}"""


# True once a transcribed take is kept as a WAV file.
_KEPT_AS_WAV = """async () => {
  const hit = performance.getEntriesByType('resource').find(e => /\\/api\\.js/.test(e.name));
  const api = await import(hit ? hit.name : '/static/api.js');
  const body = await api.jsonApi('/api/transcribe/recordings');
  return body.recordings.some(r => r.status === 'transcribed' && r.mime === 'audio/wav');
}"""


# The first four bytes of the newest kept WAV take's audio.
_KEPT_AUDIO_HEAD = """async () => {
  const hit = performance.getEntriesByType('resource').find(e => /\\/api\\.js/.test(e.name));
  const api = await import(hit ? hit.name : '/static/api.js');
  const body = await api.jsonApi('/api/transcribe/recordings');
  const take = body.recordings.find(r => r.mime === 'audio/wav');
  const res = await fetch('/api/transcribe/recordings/' + take.id + '/audio');
  return new TextDecoder().decode((await res.arrayBuffer()).slice(0, 4));
}"""


# The reason the browser could not convert the newest take that carries one.
_KEPT_NOTE = """async () => {
  const hit = performance.getEntriesByType('resource').find(e => /\\/api\\.js/.test(e.name));
  const api = await import(hit ? hit.name : '/static/api.js');
  const body = await api.jsonApi('/api/transcribe/recordings');
  const take = body.recordings.find(r => r.note);
  return take ? take.note : '';
}"""


def _wav_seconds_and_peak(wav: bytes) -> tuple[float, int]:
    """Length in seconds and loudest 16-bit sample of a 44-byte-header WAV."""
    samples = struct.unpack(f"<{(len(wav) - 44) // 2}h", wav[44:])
    return len(samples) / 16000, max((abs(s) for s in samples), default=0)


def _take(page: Page) -> bytes:
    """Record one take and return the body the page uploaded."""
    page.evaluate("() => window.__dictation.toggle()")
    page.wait_for_function("() => window.__dictation.isRecording()")
    # The take needs some audio in it; the fake microphone plays a steady tone.
    page.wait_for_timeout(800)
    with page.expect_request("**/api/transcribe") as upload:
        page.evaluate("() => window.__dictation.toggle()")
    return upload.value.post_data_buffer or b""


def test_dictation_uploads_wav_a_strict_backend_transcribes(
    authed_page: Page, base_url: str, auth_token: str
) -> None:
    serve = fake_speech_backend.serve()
    fake_url, uploads = next(serve)
    set_speech(base_url, auth_token, speech_enabled=True, speech_base_url=fake_url)
    try:
        page = authed_page
        console: list[str] = []
        page.on("console", lambda msg: console.append(msg.text))
        page.goto(base_url, wait_until="domcontentloaded")
        page.evaluate(_MOUNT)

        sent = _take(page)
        assert b'filename="recording.wav"' in sent, "the browser did not convert the take to WAV"
        expect(page.locator(FIELD)).to_have_value(fake_speech_backend.TRANSCRIPT)
        assert fake_speech_backend.is_strict_wav(uploads[-1]), "the backend did not get the portable WAV"
        # The WAV came from PCM captured off the microphone stream, not a decode,
        # and holds the fake microphone's tone for about the length of the take.
        assert any("as WAV via pcm" in line for line in console), "the take was not captured as PCM"
        seconds, peak = _wav_seconds_and_peak(uploads[-1])
        assert 0.5 < seconds < 3.0 and peak > 500, f"the PCM WAV is not the take: {seconds:.2f}s, peak {peak}"
        # The same take was kept on disk beside the upload and marked transcribed.
        page.wait_for_function(_KEPT_TRANSCRIBED)
        # The kept copy is the same WAV, not the recorder's MP4 or WebM, so a
        # retry from Settings needs no conversion on any machine.
        wait_until(page, lambda: page.evaluate(_KEPT_AS_WAV), "the kept take stored as WAV")
        kept = page.evaluate(_KEPT_AUDIO_HEAD)
        assert kept.startswith("RIFF"), f"the kept take is not a WAV: {kept!r}"

        # Without PCM capture the recorder's blob is decoded to the same WAV.
        page.evaluate("""() => {
          window.AudioWorkletNode = undefined;
          AudioContext.prototype.createScriptProcessor = undefined;
          document.getElementById('e2eDictationText').value = '';
        }""")
        console.clear()
        assert b'filename="recording.wav"' in _take(page)
        expect(page.locator(FIELD)).to_have_value(fake_speech_backend.TRANSCRIPT)
        assert any("as WAV via decoded" in line for line in console), "the decode path did not run"

        page.evaluate("""() => {
          window.OfflineAudioContext = undefined;
          window.webkitOfflineAudioContext = undefined;
          document.getElementById('e2eDictationText').value = '';
        }""")
        assert b'filename="recording.wav"' not in _take(page)
        if find_ffmpeg():
            expect(page.locator(FIELD)).to_have_value(fake_speech_backend.TRANSCRIPT)
            assert fake_speech_backend.is_strict_wav(uploads[-1]), "the server did not convert the upload"
        else:
            expect(page.locator("#toast")).to_contain_text("Unsupported audio format")
            expect(page.locator("#toast")).to_contain_text("browser step that failed: Web Audio is missing")
        expect(page.locator("#toast")).not_to_contain_text("HTTP 500")
        assert any("(raw)" in line for line in console), "the raw fallback was not logged"
        # The kept copy of that take says why the browser did not convert it.
        wait_until(page, lambda: "no Web Audio" in page.evaluate(_KEPT_NOTE), "the kept take's conversion note")

        # A refused raw upload names the browser step, whether or not this PC has ffmpeg.
        page.route(
            "**/api/transcribe",
            lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail": "refused"}'),
        )
        page.evaluate("() => window.__dictation.toggle()")
        page.wait_for_function("() => window.__dictation.isRecording()")
        page.wait_for_timeout(500)
        page.evaluate("() => window.__dictation.toggle()")
        expect(page.locator("#toast")).to_contain_text("browser step that failed: Web Audio is missing")
        page.unroute("**/api/transcribe")

        # Loopback is a secure origin, so the page is told it is not one, as
        # when a phone opens Lite over plain HTTP: a tap says HTTPS is needed.
        page.add_init_script("Object.defineProperty(window, 'isSecureContext', { get: () => false })")
        page.goto(base_url, wait_until="domcontentloaded")
        page.evaluate(_MOUNT)
        page.evaluate("() => window.__dictation.toggle()")
        expect(page.locator("#toast")).to_contain_text("Recording needs HTTPS")
        assert page.evaluate("() => window.__dictation.isRecording()") is False
    finally:
        set_speech(base_url, auth_token, speech_enabled=False, speech_base_url="")
        serve.close()
