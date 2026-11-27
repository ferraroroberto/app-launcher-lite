"""Thin HTTP client for the local-llm-hub chat endpoint.

:func:`src.notifications.summarise_failure` asks the hub for a one-line root
cause of a failed Job run. This module owns that single non-streaming call to
the hub's OpenAI-shape ``POST /v1/chat/completions``. The hub binds loopback
and serves plain HTTP, and the call is a blocking ``requests`` round-trip.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.clients import _loopback_http

logger = logging.getLogger(__name__)

# Cheap, fast model for a short summary. Must be the hyphenated, versioned hub
# id: the hub does exact-match routing, no alias table.
DEFAULT_MODEL = "claude-haiku-4-5"

# A summary is a short generation, but allow headroom for a cold model loading
# on the first call after the hub boots.
_SUMMARIZE_TIMEOUT = 60.0


class LlmError(_loopback_http.LoopbackError):
    """Raised when the local-llm-hub chat endpoint is unreachable or errors."""


def chat_url(base_url: str) -> str:
    """Upstream OpenAI-shape chat endpoint for ``POST /v1/chat/completions``."""
    return f"{base_url.rstrip('/')}/v1/chat/completions"


def build_summary_payload(
    text: str,
    system_prompt: str,
    *,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the chat-completions body that asks ``system_prompt`` about ``text``."""
    payload: Dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _extract_content(body: Any) -> str:
    """Pull the assistant message text out of an OpenAI-shape completion.

    Returns ``""`` for any unexpected shape so the caller can raise a clean
    error rather than leaking a KeyError. Handles both a plain
    string ``content`` and the list-of-parts form some backends return.
    """
    try:
        message = body["choices"][0]["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        ]
        return " ".join(t for t in parts if t).strip()
    return ""


def summarize(
    base_url: str,
    text: str,
    system_prompt: str,
    *,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    timeout: float = _SUMMARIZE_TIMEOUT,
) -> str:
    """Return the hub's short answer to ``system_prompt`` about ``text``.

    Raises :class:`LlmError` (status 503) when the hub is unreachable, the
    upstream status when it answers ``>= 400``, or 502 when it returns an
    unparseable / empty completion.
    """
    body = _loopback_http.request(
        "POST",
        chat_url(base_url),
        error=LlmError,
        service="local-llm-hub",
        timeout=timeout,
        json=build_summary_payload(text, system_prompt, model=model, max_tokens=max_tokens),
        allow_empty=False,
    )
    summary = _extract_content(body)
    if not summary:
        raise LlmError("local-llm-hub returned an empty summary")
    return summary
