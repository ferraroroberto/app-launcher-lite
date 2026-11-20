"""Speech to text: the Settings fields, the upload proxy and the connection test."""

from __future__ import annotations

import pytest

from src.clients import speech as speech_client
from src.webapp_config import load_webapp_config


@pytest.fixture(autouse=True)
def _loopback(monkeypatch):
    """Treat the TestClient host as loopback so the passkey gate on
    /api/transcribe is skipped (the gate itself has its own tests)."""
    from src import middleware
    monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))


def _upload(client, data=b"audio"):
    return client.post("/api/transcribe", files={"file": ("r.webm", data, "audio/webm")})


def test_settings_round_trip_and_key_is_write_only(webapp_client):
    client, app, overrides = webapp_client
    assert client.get("/api/config").json()["speech"] == {
        "enabled": False, "base_url": "", "model": "", "api_key_set": False,
    }
    resp = client.post("/api/config", json={
        "speech_enabled": True, "speech_base_url": " http://127.0.0.1:8000/v1 ",
        "speech_model": "whisper-1", "speech_api_key": "sekret",
    })
    assert resp.status_code == 200
    body = client.get("/api/config").json()
    assert body["speech"] == {
        "enabled": True, "base_url": "http://127.0.0.1:8000/v1",
        "model": "whisper-1", "api_key_set": True,
    }
    assert "sekret" not in client.get("/api/config").text
    assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).speech_api_key == "sekret"


@pytest.mark.parametrize("patch", [{"speech_base_url": "ftp://x"}, {"speech_base_url": "nohost"},
                                   {"speech_enabled": "false"}])
def test_settings_refuse_bad_values(webapp_client, patch):
    client, app, _ = webapp_client
    assert client.post("/api/config", json=patch).status_code == 400
    assert app.state.webapp_config.speech_base_url == ""


def test_transcribe_is_503_while_off(webapp_client):
    client, _, _ = webapp_client
    assert _upload(client).status_code == 503


def test_transcribe_forwards_to_the_configured_endpoint(webapp_client, monkeypatch):
    client, _, _ = webapp_client
    client.post("/api/config", json={"speech_enabled": True, "speech_base_url": "http://h:1/v1",
                                     "speech_model": "m", "speech_api_key": "k"})
    seen = {}

    def fake(base, model, key, name, content, ctype):
        seen.update(base=base, model=model, key=key, content=content)
        return {"transcript": "hi", "silent": False}

    monkeypatch.setattr(speech_client, "transcribe", fake)
    resp = _upload(client)
    assert resp.status_code == 200 and resp.json() == {"transcript": "hi", "silent": False}
    assert seen == {"base": "http://h:1/v1", "model": "m", "key": "k", "content": b"audio"}
    assert _upload(client, b"").status_code == 400


def test_transcribe_surfaces_endpoint_errors(webapp_client, monkeypatch):
    client, _, _ = webapp_client
    client.post("/api/config", json={"speech_enabled": True, "speech_base_url": "http://h:1/v1"})

    def boom(*a):
        raise speech_client.SpeechError("speech endpoint unreachable", status=503)

    monkeypatch.setattr(speech_client, "transcribe", boom)
    resp = _upload(client)
    assert resp.status_code == 503 and "unreachable" in resp.json()["detail"]


def test_connection_test_uses_saved_settings_only(webapp_client, monkeypatch):
    client, _, _ = webapp_client
    assert client.post("/api/speech/test").status_code == 400
    client.post("/api/config", json={"speech_base_url": "http://h:1/v1", "speech_api_key": "k"})
    monkeypatch.setattr(speech_client, "check_connection",
                        lambda base, key: {"state": "reachable", "detail": f"{base} {key}"})
    resp = client.post("/api/speech/test", json={"base_url": "http://evil/v1"})
    assert resp.json() == {"state": "reachable", "detail": "http://h:1/v1 k"}
