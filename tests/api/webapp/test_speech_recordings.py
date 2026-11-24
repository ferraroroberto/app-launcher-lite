"""Dictation takes kept on disk while recorded, and recovered from Settings."""

from __future__ import annotations

import os
import time

import pytest

from src import recordings
from src.clients import speech_audio
from tests import fake_speech_backend

BASE = "/api/transcribe/recordings"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Loopback TestClient (the passkey gate has its own tests) and a private store."""
    from src import middleware
    monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    monkeypatch.setenv("LAUNCHER_AUDIT_DIR", str(tmp_path))


def _on(client, **extra):
    client.post("/api/config", json={"speech_enabled": True, "speech_base_url": "http://127.0.0.1:9/v1", **extra})


def _start(client, mime="audio/webm"):
    take_id = client.post(BASE, json={"mime": mime}).json()["id"]
    assert take_id
    return take_id


def _chunk(client, take_id, data, ms):
    return client.post(f"{BASE}/{take_id}/chunk?ms={ms}", content=data,
                       headers={"Content-Type": "application/octet-stream"})


def test_an_abandoned_take_leaves_every_chunk_playable(webapp_client):
    client, _, _ = webapp_client
    _on(client)
    take_id = _start(client)
    for i, part in enumerate((b"one-", b"two-", b"three")):
        assert _chunk(client, take_id, part, (i + 1) * 3000).status_code == 200
    # The page dies here: no finish ever arrives.
    audio = client.get(f"{BASE}/{take_id}/audio")
    assert audio.status_code == 200 and audio.content == b"one-two-three"
    (row,) = client.get(BASE).json()["recordings"]
    assert row["status"] == "recording" and row["ms"] == 9000 and row["bytes"] == 13
    (later,) = recordings.list_takes(now=time.time() + recordings.STALE_AFTER_S + 5)
    assert later["status"] == "incomplete"


def test_a_failed_take_transcribes_once_the_endpoint_works(webapp_client):
    client, _, _ = webapp_client
    _on(client)
    take_id = _start(client, "audio/wav")
    assert _chunk(client, take_id, speech_audio.silent_wav(), 500).status_code == 200
    client.post(f"{BASE}/{take_id}/finish", json={"status": "failed", "error": "HTTP 503"})
    assert client.get(BASE).json()["recordings"][0]["status"] == "failed"

    # Still down: the retry fails and the take stays failed with the reason.
    resp = client.post(f"{BASE}/{take_id}/transcribe")
    assert resp.status_code == 503
    (row,) = client.get(BASE).json()["recordings"]
    assert row["status"] == "failed" and "unreachable" in row["error"]

    serve = fake_speech_backend.serve()
    fake_url, uploads = next(serve)
    try:
        _on(client, speech_base_url=fake_url)
        resp = client.post(f"{BASE}/{take_id}/transcribe")
        assert resp.status_code == 200 and resp.json()["transcript"] == fake_speech_backend.TRANSCRIPT
        assert fake_speech_backend.is_strict_wav(uploads[-1])
    finally:
        serve.close()
    assert client.get(BASE).json()["recordings"][0]["status"] == "transcribed"


def test_a_finished_take_takes_no_more_chunks_and_ids_are_checked(webapp_client):
    client, _, _ = webapp_client
    _on(client)
    take_id = _start(client)
    _chunk(client, take_id, b"x", 100)
    assert client.post(f"{BASE}/{take_id}/finish", json={"status": "transcribed"}).status_code == 200
    assert _chunk(client, take_id, b"y", 200).status_code == 409
    assert client.post(f"{BASE}/{take_id}/finish", json={"status": "bogus"}).status_code == 400
    assert _chunk(client, "../../config", b"y", 1).status_code == 404
    assert client.get(f"{BASE}/{'0' * 32}/audio").status_code == 404


def test_retention_prunes_old_takes_and_zero_keeps_none(webapp_client):
    client, _, _ = webapp_client
    _on(client)
    old = recordings.create("audio/webm", 24, now=time.time() - 25 * 3600)
    recordings.append(old, b"old", 1)
    stale = time.time() - 25 * 3600
    for path in recordings.recordings_dir().glob(f"{old}.*"):
        os.utime(path, (stale, stale))
    fresh = _start(client)
    assert [r["id"] for r in client.get(BASE).json()["recordings"]] == [fresh]

    assert client.post("/api/config", json={"speech_keep_recordings_hours": 0}).status_code == 200
    assert client.post(BASE, json={"mime": "audio/webm"}).json() == {"id": None}
    assert client.get(BASE).json()["recordings"] == []
    assert client.post("/api/config", json={"speech_keep_recordings_hours": 721}).status_code == 400
    assert client.post("/api/config", json={"speech_keep_recordings_hours": 1.5}).status_code == 400


def _mp4_take(client):
    """A kept MP4 take: a container no strict backend decodes, and one the server cannot convert."""
    take_id = _start(client, "audio/mp4")
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    assert _chunk(client, take_id, mp4, 500).status_code == 200
    return take_id


@pytest.fixture
def strict_backend_no_ffmpeg(monkeypatch):
    """A strict speech backend on a PC with no ffmpeg."""
    monkeypatch.setattr(speech_audio, "find_ffmpeg", lambda *a, **k: None)
    gen = fake_speech_backend.serve()
    yield next(gen)
    gen.close()


def test_a_kept_mp4_take_transcribes_from_the_browsers_wav_with_no_ffmpeg(
    webapp_client, strict_backend_no_ffmpeg,
):
    client, _, _ = webapp_client
    base, uploads = strict_backend_no_ffmpeg
    client.post("/api/config", json={"speech_enabled": True, "speech_base_url": base})
    take_id = _mp4_take(client)
    resp = client.post(f"{BASE}/{take_id}/transcribe",
                       files={"file": ("recording.wav", speech_audio.silent_wav(), "audio/wav")})
    assert resp.status_code == 200 and resp.json()["transcript"] == fake_speech_backend.TRANSCRIPT
    assert fake_speech_backend.is_strict_wav(uploads[-1])
    (row,) = client.get(BASE).json()["recordings"]
    assert row["status"] == "transcribed"


def test_without_the_browsers_wav_the_server_path_is_unchanged_and_fails_plainly(
    webapp_client, strict_backend_no_ffmpeg,
):
    client, _, _ = webapp_client
    base, uploads = strict_backend_no_ffmpeg
    client.post("/api/config", json={"speech_enabled": True, "speech_base_url": base})
    take_id = _mp4_take(client)
    resp = client.post(f"{BASE}/{take_id}/transcribe")
    assert resp.status_code == 415
    detail = resp.json()["detail"]
    assert "Unsupported audio format" in detail and "mp4" in detail
    assert "browser could not convert" in detail and "Installing ffmpeg on this PC" in detail
    assert uploads[-1].startswith(b"\x00\x00\x00\x18ftyp"), "the stored recording goes as it was kept"
    (row,) = client.get(BASE).json()["recordings"]
    assert row["status"] == "failed" and "Installing ffmpeg" in row["error"]


def test_a_take_the_browser_could_not_convert_keeps_the_reason(webapp_client, strict_backend_no_ffmpeg):
    client, _, _ = webapp_client
    base, _ = strict_backend_no_ffmpeg
    client.post("/api/config", json={"speech_enabled": True, "speech_base_url": base})
    take_id = _mp4_take(client)
    reason = "this browser could not decode its own audio/mp4 recording (EncodingError)"
    client.post(f"{BASE}/{take_id}/finish", json={"status": "failed", "error": "refused", "note": reason})
    (row,) = client.get(BASE).json()["recordings"]
    assert row["note"] == reason
    # A retry from Settings leaves the live take's reason in place.
    assert client.post(f"{BASE}/{take_id}/transcribe").status_code == 415
    (row,) = client.get(BASE).json()["recordings"]
    assert row["note"] == reason
    # A take the browser did convert reports an empty note, which replaces it.
    client.post(f"{BASE}/{take_id}/finish", json={"status": "transcribed", "note": ""})
    (row,) = client.get(BASE).json()["recordings"]
    assert row["note"] == ""
