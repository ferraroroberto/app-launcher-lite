"""Client for an OpenAI-shaped speech-to-text endpoint.

The base URL is whatever the owner configured (``http://127.0.0.1:8000/v1``
for the local hub, another machine's server, or any OpenAI-compatible host), so
unlike the loopback sibling clients it verifies TLS certificates.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from src.clients import _loopback_http, speech_audio

logger = logging.getLogger(__name__)

# A cold whisper on the first take after boot is slow.
_TRANSCRIBE_TIMEOUT = 60.0
# The reachability half of the connection test: a server that has not
# answered in this long is reported as unknown, never waited on.
_PROBE_TIMEOUT = 5.0
# The transcription half sends half a second of silence; a cold model may
# take a while to load before it answers.
_PROBE_TRANSCRIBE_TIMEOUT = 20.0

STATE_TRANSCRIBES = "transcribes"
STATE_REFUSES_AUDIO = "refuses_audio"
STATE_REACHABLE = "reachable"
STATE_REFUSED = "refused"
STATE_UNKNOWN = "unknown"


class SpeechError(_loopback_http.LoopbackError):
    """Raised when the speech endpoint is unreachable or returns an error."""


class AudioRefusedError(SpeechError):
    """The backend answered, but would not transcribe the audio it was sent."""


def _headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _backend_detail(resp: Any) -> str:
    """The backend's own words for an error: a JSON ``detail``, an OpenAI
    ``error.message``, or the start of a plain-text body."""
    try:
        body = resp.json()
    except ValueError:
        return (getattr(resp, "text", "") or "").strip()[:200]
    if isinstance(body, dict):
        if body.get("detail"):
            return str(body["detail"])[:200]
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
    return ""


def transcribe(
    base_url: str,
    model: str,
    api_key: str,
    filename: str,
    content: bytes,
    content_type: str,
    timeout: float = _TRANSCRIBE_TIMEOUT,
) -> Dict[str, Any]:
    """Transcribe one audio file and return ``{"transcript", "silent"}``.

    ``model`` and ``api_key`` are sent only when set. Each failure raises its
    own message: unreachable (503), no answer in time (504), the key rejected
    (the upstream 401/403), the audio refused (:class:`AudioRefusedError`,
    502, with the backend's detail) or a reply that is not the OpenAI
    ``{"text": ...}`` shape (502).
    """
    url = f"{base_url.rstrip('/')}/audio/transcriptions"
    try:
        resp = _loopback_http.pooled_request(
            "POST",
            url,
            timeout=timeout,
            headers=_headers(api_key),
            data={"model": model} if model else None,
            files={"file": (filename, content, content_type)},
        )
    except requests.exceptions.ReadTimeout as exc:
        raise SpeechError(f"The speech backend did not answer within {timeout:.0f}s.", status=504) from exc
    except requests.RequestException as exc:
        raise SpeechError(f"Speech backend unreachable at {url} ({type(exc).__name__}).", status=503) from exc
    code = resp.status_code
    if code >= 400:
        detail = _backend_detail(resp)
        suffix = f": {detail}" if detail else ""
        if code in (401, 403):
            raise SpeechError(f"The speech backend rejected the API key (HTTP {code}){suffix}.", status=code)
        raise AudioRefusedError(f"The speech backend refused the audio (HTTP {code}){suffix}.", status=502)
    try:
        body = resp.json()
    except ValueError as exc:
        raise SpeechError("The speech backend's reply is not JSON.") from exc
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise SpeechError("The speech backend's reply has no 'text' field.")
    text = text.strip()
    return {"transcript": text, "silent": not text}


def transcribe_upload(
    base_url: str, model: str, api_key: str, filename: str, content: bytes, content_type: str,
) -> Dict[str, Any]:
    """Transcribe a browser upload, converting it to the portable WAV first
    when it is in another shape and ffmpeg is available.

    Without ffmpeg the upload goes as is; if the backend then refuses it, the
    error names it an unsupported audio format (415), never a bare 500.
    """
    info = speech_audio.describe(content)
    logger.info("ℹ️ transcription upload: container=%s rate=%s channels=%s bytes=%d",
                info["container"], info["rate"], info["channels"], len(content))
    portable = speech_audio.is_portable_wav(info)
    if not portable:
        ffmpeg = speech_audio.find_ffmpeg()
        if ffmpeg:
            try:
                content = speech_audio.to_portable_wav(content, ffmpeg)
            except speech_audio.AudioDecodeError as exc:
                raise SpeechError(f"Unsupported audio format: ffmpeg could not decode the "
                                  f"{info['container']} recording ({exc}).", status=415) from exc
            filename, content_type, portable = "recording.wav", "audio/wav", True
            logger.info("ℹ️ converted the %s upload to 16 kHz mono WAV (%d bytes)",
                        info["container"], len(content))
    try:
        return transcribe(base_url, model, api_key, filename, content, content_type)
    except AudioRefusedError as exc:
        if portable:
            raise
        raise SpeechError(f"Unsupported audio format: {exc} The recording was "
                          f"{info['container']}. This browser could not convert it to 16 kHz mono "
                          "WAV, and ffmpeg is not on this PC's PATH, so the server could not "
                          "either. Installing ffmpeg on this PC lets the server do it.",
                          status=415) from exc


def _was_refused(exc: BaseException) -> bool:
    """True when a ConnectionRefusedError sits anywhere in the exception chain."""
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, ConnectionRefusedError):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def _probe_server(base_url: str, api_key: str) -> Dict[str, str]:
    """``GET <base>/models``: proves a server is there, nothing more."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = _loopback_http.pooled_request(
            "GET", url, timeout=_PROBE_TIMEOUT, headers=_headers(api_key)
        )
    except requests.RequestException as exc:
        if _was_refused(exc):
            return {"state": STATE_REFUSED, "detail": f"Connection refused at {url}: nothing is listening there."}
        logger.warning("⚠️ speech endpoint probe at %s failed: %s", url, exc)
        return {"state": STATE_UNKNOWN, "detail": f"No answer from {url} ({type(exc).__name__})."}
    code = resp.status_code
    if code in (401, 403):
        return {"state": STATE_REFUSED, "detail": f"The server rejected the API key (HTTP {code})."}
    if code >= 500:
        return {"state": STATE_UNKNOWN, "detail": f"The server answered with an error (HTTP {code})."}
    return {"state": STATE_REACHABLE, "detail": f"The server answered (HTTP {code})."}


def check_connection(base_url: str, model: str, api_key: str) -> Dict[str, str]:
    """Probe the endpoint and report ``{"state", "detail"}``.

    ``transcribes``: a half-second silent WAV came back transcribed.
    ``refuses_audio``: the server answered but would not transcribe it.
    ``reachable``: a server answered, but whether it transcribes could not
    be established. ``refused``: nothing is listening, or the key was
    rejected. ``unknown``: no verdict at all.
    """
    server = _probe_server(base_url, api_key)
    if server["state"] == STATE_REFUSED:
        return server
    try:
        transcribe(base_url, model, api_key, "probe.wav", speech_audio.silent_wav(),
                   "audio/wav", timeout=_PROBE_TRANSCRIBE_TIMEOUT)
    except AudioRefusedError as exc:
        return {"state": STATE_REFUSES_AUDIO, "detail": f"{exc} It was sent a 16 kHz mono WAV, the most portable shape."}
    except SpeechError as exc:
        if exc.status in (401, 403):
            return {"state": STATE_REFUSED, "detail": str(exc)}
        logger.warning("⚠️ speech transcription probe failed: %s", exc)
        if server["state"] == STATE_REACHABLE:
            return {"state": STATE_REACHABLE, "detail": f"{server['detail']} Transcription not confirmed: {exc}"}
        return {"state": STATE_UNKNOWN, "detail": str(exc)}
    return {"state": STATE_TRANSCRIBES, "detail": "A short test recording was transcribed."}
