"""Dictation reaches a speech backend that decodes only 16 kHz mono WAV.

Chromium's fake microphone feeds the real MediaRecorder and Web Audio path of
`voice.js`; the take goes through the disposable webapp's `/api/transcribe` to
`tests/fake_speech_backend.py`, which answers an opaque HTTP 500 to anything
but that WAV. With Web Audio removed the page uploads the recorder's own
container: the server then converts it (ffmpeg on PATH) or says the format is
unsupported, never a bare HTTP 500.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from src.clients.speech_audio import find_ffmpeg
from tests import fake_speech_backend
from tests.e2e.settings._speech import set_speech

# Chromium only: WebKit has no fake microphone.
pytestmark = pytest.mark.smoke

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
        page.goto(base_url, wait_until="domcontentloaded")
        page.evaluate(_MOUNT)

        sent = _take(page)
        assert b'filename="recording.wav"' in sent, "the browser did not convert the take to WAV"
        expect(page.locator(FIELD)).to_have_value(fake_speech_backend.TRANSCRIPT)
        assert fake_speech_backend.is_strict_wav(uploads[-1]), "the backend did not get the portable WAV"
        # The same take was kept on disk beside the upload and marked transcribed.
        page.wait_for_function(_KEPT_TRANSCRIBED)

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
        expect(page.locator("#toast")).not_to_contain_text("HTTP 500")
    finally:
        set_speech(base_url, auth_token, speech_enabled=False, speech_base_url="")
        serve.close()
