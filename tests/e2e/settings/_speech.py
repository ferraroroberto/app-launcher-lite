"""Settings > Speech to text, walked inside the Settings sheets test: the sheet
saves each field, the row value follows the switch, and the connection test
shows each of its three states; Recent recordings plays, transcribes and copies a
kept take, converting it to WAV in the browser first."""

from __future__ import annotations

import io
import json
import re
import wave
from pathlib import Path

from playwright.sync_api import Page, expect

from src.clients.speech_audio import silent_wav
from tests import fake_speech_backend
from tests.e2e.fixtures.pages import loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import open_settings, open_settings_sheet


def set_speech(base_url: str, auth_token: str, **patch: object) -> None:
    """Write speech settings straight to the disposable webapp's config."""
    loopback("POST", base_url + "/api/config", headers=auth_headers(auth_token),
             json=patch).raise_for_status()


def _stub_probe(state: str):
    return lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"state": state, "detail": "stubbed"}))


def _keep_take(base_url: str, auth_token: str) -> str:
    """Leave one kept take on the disposable webapp, as a page that died mid-take would."""
    base = base_url + "/api/transcribe/recordings"
    take_id = loopback("POST", base, headers=auth_headers(auth_token),
                       json={"mime": "audio/wav"}).json()["id"]
    loopback("POST", f"{base}/{take_id}/chunk?ms=500", data=silent_wav(),
             headers={**auth_headers(auth_token), "Content-Type": "application/octet-stream"}
             ).raise_for_status()
    return take_id


def walk_recent_recordings(page: Page, base_url: str, auth_token: str) -> None:
    """Recent recordings: a kept take plays, transcribes and copies; the main
    tabs gain nothing."""
    take_id = _keep_take(base_url, auth_token)
    page.locator("#speechRecordingsRefresh").click()
    row = page.locator(f'#speechRecordings [data-recording-id="{take_id}"]')
    expect(row.locator(".speech-recording-status")).to_have_text("Recording")
    row.get_by_role("button", name="Play").click()
    expect(row.locator("audio")).to_have_attribute("src", re.compile(r"^blob:"))
    # Port 9 refuses: the retry fails and says so.
    row.get_by_role("button", name="Transcribe").click()
    expect(row.locator(".speech-recording-status")).to_have_text("Failed")
    page.route(f"**/api/transcribe/recordings/{take_id}/transcribe", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"transcript": "kept words", "silent": False})))
    row.get_by_role("button", name="Transcribe").click()
    expect(row.locator(".speech-recording-text")).to_have_text("kept words")
    expect(row.locator(".speech-recording-status")).to_have_text("Transcribed")
    row.get_by_role("button", name="Copy").click()
    expect(page.locator("#toast")).to_contain_text("Transcript")
    page.unroute(f"**/api/transcribe/recordings/{take_id}/transcribe")


def _stereo_wav() -> bytes:
    """A take in a shape no strict backend reads and the server cannot convert without ffmpeg."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44100)
        out.writeframes(b"\x00\x10" * 2 * 4410)
    return buf.getvalue()


def walk_kept_take_converts_in_the_browser(page: Page, base_url: str, auth_token: str) -> None:
    """Transcribe on a kept take that is not the portable WAV reaches a strict
    backend as the browser's own WAV, so it needs no ffmpeg on this PC."""
    serve = fake_speech_backend.serve()
    fake_url, uploads = next(serve)
    set_speech(base_url, auth_token, speech_base_url=fake_url)
    try:
        base = base_url + "/api/transcribe/recordings"
        take_id = loopback("POST", base, headers=auth_headers(auth_token), json={"mime": "audio/wav"}).json()["id"]
        loopback("POST", f"{base}/{take_id}/chunk?ms=500", data=_stereo_wav(),
                 headers={**auth_headers(auth_token), "Content-Type": "application/octet-stream"}
                 ).raise_for_status()
        page.locator("#speechRecordingsRefresh").click()
        row = page.locator(f'#speechRecordings [data-recording-id="{take_id}"]')
        with page.expect_request(lambda r: r.url.endswith(f"/{take_id}/transcribe")) as sent:
            row.get_by_role("button", name="Transcribe").click()
        assert b'filename="recording.wav"' in (sent.value.post_data_buffer or b""), "the page sent no converted WAV"
        expect(row.locator(".speech-recording-text")).to_have_text(fake_speech_backend.TRANSCRIPT)
        assert fake_speech_backend.is_strict_wav(uploads[-1])
    finally:
        set_speech(base_url, auth_token, speech_base_url="http://127.0.0.1:9/v1")
        serve.close()


PHONE_TAKE = Path(__file__).resolve().parents[2] / "fixtures" / "phone_take.mp4"


def _keep_audio(base_url: str, auth_token: str, mime: str, audio: bytes) -> str:
    """Leave one kept take holding `audio` as the recorder wrote it."""
    base = base_url + "/api/transcribe/recordings"
    take_id = loopback("POST", base, headers=auth_headers(auth_token), json={"mime": mime}).json()["id"]
    loopback("POST", f"{base}/{take_id}/chunk?ms=1000", data=audio,
             headers={**auth_headers(auth_token), "Content-Type": "application/octet-stream"}
             ).raise_for_status()
    return take_id


def walk_kept_phone_take_transcribes_and_failures_say_why(
    page: Page, base_url: str, auth_token: str,
) -> None:
    """A kept AAC take, as a phone records it, reaches a strict backend as the
    browser's WAV; a take this browser cannot decode fails with the browser's
    own reason in its row, not only in the console."""
    serve = fake_speech_backend.serve()
    fake_url, uploads = next(serve)
    set_speech(base_url, auth_token, speech_base_url=fake_url)
    try:
        good = _keep_audio(base_url, auth_token, "audio/mp4", PHONE_TAKE.read_bytes())
        bad = _keep_audio(base_url, auth_token, "audio/mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        page.locator("#speechRecordingsRefresh").click()
        row = page.locator(f'#speechRecordings [data-recording-id="{good}"]')
        with page.expect_request(lambda r: r.url.endswith(f"/{good}/transcribe")) as sent:
            row.get_by_role("button", name="Transcribe").click()
        assert b'filename="recording.wav"' in (sent.value.post_data_buffer or b""), "the page sent no converted WAV"
        expect(row.locator(".speech-recording-text")).to_have_text(fake_speech_backend.TRANSCRIPT)
        assert fake_speech_backend.is_strict_wav(uploads[-1])

        broken = page.locator(f'#speechRecordings [data-recording-id="{bad}"]')
        broken.get_by_role("button", name="Transcribe").click()
        expect(broken.locator(".speech-recording-status")).to_have_text("Failed")
        expect(broken.locator(".speech-recording-error")).to_contain_text(
            "Browser conversion: this browser could not decode its own audio/mp4 recording (EncodingError")
    finally:
        set_speech(base_url, auth_token, speech_base_url="http://127.0.0.1:9/v1")
        serve.close()


def walk_speech_sheet(page: Page, base_url: str, auth_token: str) -> None:
    reset = dict(speech_enabled=False, speech_base_url="", speech_model="", speech_api_key="")
    set_speech(base_url, auth_token, **reset)
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        open_settings(page)
        row = page.locator('#paneSettings [data-settings-value="speech"]')
        expect(row).to_have_text("Off")
        open_settings_sheet(page, "speechSheet")

        page.locator("#speechBaseUrl").fill("http://127.0.0.1:9/v1")
        page.locator("#speechBaseUrl").blur()
        page.locator("#speechApiKey").fill("fake-key-for-test")
        page.locator("#speechApiKey").blur()
        expect(page.locator("#speechApiKeyState")).to_have_text("A key is set.")
        expect(page.locator("#speechApiKey")).to_have_value("")
        page.locator("#speechEnabledToggle").click()
        expect(page.locator("#speechEnabledToggle")).to_have_attribute("aria-checked", "true")

        # Port 9 has no listener: the real server answers "refused".
        page.locator("#speechTest").click()
        expect(page.locator("#speechTestResult")).to_contain_text("Refused")

        for state in ("reachable", "unknown"):
            page.route("**/api/speech/test", _stub_probe(state))
            page.locator("#speechTest").click()
            expect(page.locator("#speechTestResult")).to_have_text(f"{state.capitalize()}: stubbed")
            page.unroute("**/api/speech/test")

        walk_recent_recordings(page, base_url, auth_token)
        walk_kept_take_converts_in_the_browser(page, base_url, auth_token)
        walk_kept_phone_take_transcribes_and_failures_say_why(page, base_url, auth_token)

        page.locator("#speechApiKeyClear").click()
        expect(page.locator("#speechApiKeyState")).to_have_text("No key set.")
        page.locator("#speechSheet .detail-actions [data-sheet-close]").click()
        expect(row).to_have_text("On")
    finally:
        # The disposable webapp is shared by the rest of the run.
        set_speech(base_url, auth_token, **reset)
