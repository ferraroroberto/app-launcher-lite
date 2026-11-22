"""The OpenAI-shaped speech endpoint client (`src.clients.speech`)."""

from __future__ import annotations

import io
import wave

import pytest
import requests

from src.clients import _loopback_http, speech, speech_audio
from tests import fake_speech_backend


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

    _stub(monkeypatch, _Resp(status_code=500, payload={"error": {"message": "cannot decode"}}))
    with pytest.raises(speech.AudioRefusedError, match="refused the audio .*cannot decode") as exc:
        _send()
    assert exc.value.status == 502

    _stub(monkeypatch, requests.ConnectionError("down"))
    with pytest.raises(speech.SpeechError, match="unreachable") as exc:
        _send()
    assert exc.value.status == 503

    _stub(monkeypatch, requests.ReadTimeout("slow"))
    with pytest.raises(speech.SpeechError, match="did not answer") as exc:
        _send()
    assert exc.value.status == 504


def _stub_by_method(monkeypatch, get_reply, post_reply):
    def fake(method, url, **kw):
        reply = get_reply if method == "GET" else post_reply
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(_loopback_http.SESSION, "request", fake)


@pytest.mark.parametrize("get_reply,post_reply,state", [
    (_Resp(200), _Resp(payload={"text": ""}), "transcribes"),
    (_Resp(404), _Resp(payload={"text": "hi"}), "transcribes"),
    (_Resp(200), _Resp(500), "refuses_audio"),
    (_Resp(200), requests.ReadTimeout("slow"), "reachable"),
    (_Resp(503), requests.ReadTimeout("slow"), "unknown"),
    (_Resp(401), _Resp(payload={"text": "hi"}), "refused"),
    (_Resp(200), _Resp(403), "refused"),
])
def test_check_connection_states(monkeypatch, get_reply, post_reply, state):
    _stub_by_method(monkeypatch, get_reply, post_reply)
    assert speech.check_connection("http://h:1/v1", "", "")["state"] == state


def test_check_connection_refused_versus_unknown(monkeypatch):
    refused = requests.ConnectionError("boom")
    refused.__cause__ = ConnectionRefusedError(10061, "refused")
    _stub(monkeypatch, refused)
    assert speech.check_connection("http://h:1/v1", "", "")["state"] == "refused"

    _stub(monkeypatch, requests.ConnectTimeout("slow"))
    assert speech.check_connection("http://h:1/v1", "", "")["state"] == "unknown"


@pytest.fixture
def strict_backend():
    gen = fake_speech_backend.serve()
    yield next(gen)
    gen.close()


def test_connection_test_proves_transcription_against_a_strict_backend(strict_backend):
    base, uploads = strict_backend
    assert speech.check_connection(base, "", "")["state"] == "transcribes"
    assert fake_speech_backend.is_strict_wav(uploads[-1])


def test_upload_without_ffmpeg_is_an_unsupported_format_not_a_bare_500(strict_backend, monkeypatch):
    monkeypatch.setattr(speech_audio, "find_ffmpeg", lambda: None)
    with pytest.raises(speech.SpeechError) as exc:
        speech.transcribe_upload(strict_backend[0], "", "", "r.webm", b"\x1aE\xdf\xa3" + b"x" * 64, "audio/webm")
    assert exc.value.status == 415
    assert "Unsupported audio format" in str(exc.value) and "webm" in str(exc.value)


@pytest.mark.skipif(not speech_audio.find_ffmpeg(), reason="needs ffmpeg on PATH")
def test_upload_in_another_shape_is_converted_with_ffmpeg(strict_backend):
    stereo = io.BytesIO()
    with wave.open(stereo, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44100)
        out.writeframes(b"\x00\x10" * 2 * 4410)
    base, uploads = strict_backend
    result = speech.transcribe_upload(base, "", "", "r.wav", stereo.getvalue(), "audio/wav")
    assert result["transcript"] == fake_speech_backend.TRANSCRIPT
    assert fake_speech_backend.is_strict_wav(uploads[-1])
