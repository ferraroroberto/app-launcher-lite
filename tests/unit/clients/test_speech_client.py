"""The OpenAI-shaped speech endpoint client (`src.clients.speech`)."""

from __future__ import annotations

import pytest
import requests

from src.clients import _loopback_http, speech


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _stub(monkeypatch, reply):
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(_loopback_http.SESSION, "request", fake)
    return calls


def _send(**over):
    args = dict(base_url="http://h:1/v1/", model="", api_key="", filename="a.webm",
                content=b"x", content_type="audio/webm")
    args.update(over)
    return speech.transcribe(**args)


def test_transcribe_posts_openai_shape_and_sends_model_and_key_only_when_set(monkeypatch):
    calls = _stub(monkeypatch, _Resp(payload={"text": " hello "}))
    assert _send() == {"transcript": "hello", "silent": False}
    method, url, kw = calls[0]
    assert (method, url) == ("POST", "http://h:1/v1/audio/transcriptions")
    assert kw["data"] is None and kw["headers"] == {}
    assert kw["verify"] is True  # a configured, possibly remote host: certs are checked

    calls = _stub(monkeypatch, _Resp(payload={"text": "x"}))
    _send(model="whisper-1", api_key="k")
    assert calls[0][2]["data"] == {"model": "whisper-1"}
    assert calls[0][2]["headers"] == {"Authorization": "Bearer k"}


def test_transcribe_blank_text_is_silent(monkeypatch):
    _stub(monkeypatch, _Resp(payload={"text": "  "}))
    assert _send() == {"transcript": "", "silent": True}


def test_transcribe_maps_upstream_failures(monkeypatch):
    _stub(monkeypatch, _Resp(status_code=401, payload={"detail": "bad key"}))
    with pytest.raises(speech.SpeechError) as exc:
        _send()
    assert exc.value.status == 401 and "bad key" in str(exc.value)

    _stub(monkeypatch, _Resp(payload={"nope": 1}))
    with pytest.raises(speech.SpeechError, match="no 'text'"):
        _send()

    _stub(monkeypatch, requests.ConnectionError("down"))
    with pytest.raises(speech.SpeechError) as exc:
        _send()
    assert exc.value.status == 503


@pytest.mark.parametrize("code,state", [
    (200, "reachable"), (404, "reachable"), (401, "refused"), (403, "refused"), (503, "unknown"),
])
def test_check_connection_http_states(monkeypatch, code, state):
    calls = _stub(monkeypatch, _Resp(status_code=code))
    assert speech.check_connection("http://h:1/v1", "")["state"] == state
    assert calls[0][1] == "http://h:1/v1/models"


def test_check_connection_refused_versus_unknown(monkeypatch):
    refused = requests.ConnectionError("boom")
    refused.__cause__ = ConnectionRefusedError(10061, "refused")
    _stub(monkeypatch, refused)
    assert speech.check_connection("http://h:1/v1", "")["state"] == "refused"

    _stub(monkeypatch, requests.ConnectTimeout("slow"))
    assert speech.check_connection("http://h:1/v1", "")["state"] == "unknown"
