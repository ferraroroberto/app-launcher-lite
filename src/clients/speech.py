"""Client for an OpenAI-shaped speech-to-text endpoint.

The base URL is whatever the owner configured (``http://127.0.0.1:8000/v1``
for the local hub, another machine's server, or any OpenAI-compatible host), so
unlike the loopback sibling clients it verifies TLS certificates.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from src.clients import _loopback_http

logger = logging.getLogger(__name__)

# A cold whisper plus an ffmpeg transcode on the first take after boot is slow.
_TRANSCRIBE_TIMEOUT = 60.0
# The connection test is a quick probe: a server that has not answered in this
# long is reported as unknown, never waited on.
_PROBE_TIMEOUT = 5.0

STATE_REACHABLE = "reachable"
STATE_REFUSED = "refused"
STATE_UNKNOWN = "unknown"


class SpeechError(_loopback_http.LoopbackError):
    """Raised when the speech endpoint is unreachable or returns an error."""


def _headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def transcribe(
    base_url: str,
    model: str,
    api_key: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> Dict[str, Any]:
    """Transcribe one recorded audio blob and return ``{"transcript", "silent"}``.

    ``model`` and ``api_key`` are sent only when set. Raises
    :class:`SpeechError` (carrying an HTTP status) on a transport failure, an
    upstream error or a reply that is not the OpenAI ``{"text": ...}`` shape.
    """
    url = f"{base_url.rstrip('/')}/audio/transcriptions"
    data = {"model": model} if model else None
    body = _loopback_http.request(
        "POST",
        url,
        error=SpeechError,
        service="speech endpoint",
        timeout=_TRANSCRIBE_TIMEOUT,
        allow_empty=False,
        headers=_headers(api_key),
        data=data,
        files={"file": (filename, content, content_type)},
    )
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise SpeechError("speech endpoint reply has no 'text' field")
    text = text.strip()
    return {"transcript": text, "silent": not text}


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


def check_connection(base_url: str, api_key: str) -> Dict[str, str]:
    """Probe the endpoint and report ``{"state", "detail"}``.

    ``reachable``: a server answered. ``refused``: nothing is listening, or the
    server rejected the key (401/403). ``unknown``: no verdict could be
    established (timeout, DNS or TLS failure, a 5xx). The probe is
    ``GET <base>/models``, which proves a server is there, not that
    transcription works; only a real recording proves that.
    """
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
    return {"state": STATE_REACHABLE, "detail": f"The server answered (HTTP {code}). A recording is the only proof that transcription works."}
