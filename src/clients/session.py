"""Thin HTTP client for the loopback PTY session-host.

The webapp owns all auth, Tailscale gating, and WebAuthn; it talks to the
session-host (``app/webapp/session_host/server.py``) purely over loopback. These
are blocking ``requests`` calls — webapp routes wrap them in
``asyncio.to_thread`` so the event loop never stalls. The WebSocket proxy
is handled separately in ``app/webapp/server.py``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import requests

from src.clients import _loopback_http
from src.session_view import folder_trust

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0
# Spawning a PTY session is slow (cold pywinpty + agent cold-start can take
# 10–20 s on a freshly booted box). Reuse of the 8 s default here was
# surfacing 'session-host unreachable' to the phone while the spawn was
# still in flight, prompting retries that stacked orphan sessions.
_CREATE_TIMEOUT = 45.0
# A bulk write waits for the paste's ingest to become visible before it will
# submit (up to _INGEST_CAP_MS) on top of write()'s own
# chunk-and-pace pauses, so the input call needs headroom over the 8 s
# default — otherwise the client times out first and the caller sees
# "session-host unreachable" instead of the host's actual delivery verdict,
# which is the whole point of the verdict.
_INPUT_TIMEOUT = 15.0
# A graceful stop polls for the agent to exit on its quit command (up to the
# host's ~5 s grace window) before force-falling-back, so the stop call needs
# more headroom than the 8 s default.
_STOP_TIMEOUT = 10.0

# The one delivery ``reason`` the webapp itself has to branch on,
# to mirror the session-host's 202 for a submit that is still with its
# background watcher. Restated here rather than imported because the webapp
# process deliberately never imports ``src.session_host`` — that module owns
# live ConPTYs and belongs to the *other* process; this client is the webapp's
# whole view of the protocol. ``tests/unit/session_host/test_session_host_input_endpoint.py``
# pins the two definitions equal so they cannot drift apart silently.
INPUT_DEFERRED = "deferred"

# A just-spawned Copilot discards text written before it is ready, and the
# host still reports such a write as delivered. Probed live: the TUI takes over
# the terminal 4-5 s after the spawn (bracketed-paste mode and the window title
# arrive within ~0.1 s of each other), text sent at that instant is lost, and
# text sent a second or so later lands. So a session younger than the window is
# held until it has set its title (the TUI is up) and then stopped painting
# its boot screen for ``_READY_QUIET_S``; the quiet rule adapts to a slow,
# loaded boot where a fixed delay would not.
_READY_WINDOW_S = 60.0
_READY_QUIET_S = 1.0
# How long a send waits for that before giving up: refused when the TUI never
# came up (the text would be lost), sent anyway when it is up but still busy
# (an agent already working takes type-ahead).
_READY_WAIT_S = 15.0
_READY_POLL_S = 0.2

# A large-file upload streams for as long as the phone takes to send
# it, so it has no overall deadline: each connect, write and read waits at
# most this long, and the read covers the host's final flush to disk.
_LARGE_UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def ws_url(port: int, session_id: str, role: str = "phone") -> str:
    return f"ws://127.0.0.1:{port}/sessions/{session_id}/ws?role={role}"


class SessionHostError(_loopback_http.LoopbackError):
    """Raised when the session-host is unreachable or returns an error."""


class SessionNotReady(SessionHostError):
    """A session's agent has not drawn its input box yet: nothing was sent.

    A 409 so a caller can tell "retry shortly" from a lost message; the
    ``not_ready`` marker lets the route audit it under its own reason without
    importing this class.
    """

    not_ready = True


class FolderTrustPending(SessionHostError):
    """The session is sitting on Copilot's folder-trust dialog: nothing was sent.

    A 409 so a caller can tell "the owner must trust this folder" from "retry
    shortly" and from a lost message; ``needs_folder_trust`` lets the route
    audit it under its own reason without importing this class.
    """

    needs_folder_trust = True

    def __init__(self, session_id: str, folder: str) -> None:
        self.folder = folder
        super().__init__(
            f'session {session_id} needs folder trust: Copilot is asking whether to '
            f'trust "{folder}" and cannot read a message until it is answered, so '
            f'this was NOT delivered, nothing was sent. Trust the folder (Board: '
            f'"Trust this folder"), then resend',
            status=409,
        )


def _request(method: str, port: int, path: str, *, timeout: float = _TIMEOUT, **kwargs) -> Any:
    return _loopback_http.request(
        method,
        base_url(port) + path,
        error=SessionHostError,
        service="session-host",
        timeout=timeout,
        **kwargs,
    )


def identity(port: int) -> Optional[Dict[str, Any]]:
    """The session-host's ``/healthz`` body (``git_sha``/``started_at``),
    or ``None`` when unreachable — the build identity ``/api/version`` uses to
    report whether the session-host is running current code."""
    try:
        resp = _loopback_http.pooled_request(
            "GET", base_url(port) + "/healthz", timeout=2.0
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None
    except (requests.RequestException, ValueError):
        return None


def create_session(
    port: int,
    project_dir: str,
    name: str,
    flags: str,
    kind: str = "pty",
    agent: str = "claude",
    rows: int = 40,
    cols: int = 120,
    history_lines: Optional[int] = None,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "project_dir": project_dir,
        "name": name,
        "flags": flags,
        "kind": kind,
        "agent": agent,
        # Phone's real terminal size, so the PTY's first frame is the
        # right width for a ratatui TUI.
        "rows": rows,
        "cols": cols,
    }
    # User-configurable scrollback depth for full-screen agents.
    # Omitted → the session-host's own default.
    if history_lines is not None:
        payload["history_lines"] = history_lines
    # Role tag (e.g. "chief"). Omitted when unset — a legacy host
    # ignores unknown keys, so this stays backward compatible either way.
    if label:
        payload["label"] = label
    return _request(
        "POST",
        port,
        "/sessions",
        timeout=_CREATE_TIMEOUT,
        json=payload,
    )


def list_sessions(port: int) -> List[Dict[str, Any]]:
    data = _request("GET", port, "/sessions")
    return list(data.get("sessions") or [])


def get_session(port: int, session_id: str) -> Dict[str, Any]:
    return _request("GET", port, f"/sessions/{session_id}")


_BOOT_DONE = "done"
_BOOT_NO_TUI = "no_tui"
_BOOT_PAINTING = "painting"


def _boot_state(info: Dict[str, Any]) -> str:
    """Where a session is in its start-up: ``done`` unless it is a live,
    young Copilot whose TUI is not up (``no_tui``) or still painting its boot
    screen (``painting``)."""
    if info.get("kind", "pty") != "pty" or info.get("agent") != "copilot":
        return _BOOT_DONE
    started_at = info.get("started_at")
    if not info.get("alive") or not isinstance(started_at, (int, float)):
        return _BOOT_DONE
    if time.time() - started_at >= _READY_WINDOW_S:
        return _BOOT_DONE
    if not info.get("live_title"):
        return _BOOT_NO_TUI
    last_output_at = info.get("last_output_at")
    if isinstance(last_output_at, (int, float)) and time.time() - last_output_at < _READY_QUIET_S:
        return _BOOT_PAINTING
    return _BOOT_DONE


def _await_tui_ready(port: int, session_id: str) -> Optional[Dict[str, Any]]:
    """Hold a write until a just-spawned Copilot reads its input.

    Returns at once for any session that is not young, and when the session
    cannot be read (the write then reports the real state). After
    ``_READY_WAIT_S`` it raises :class:`SessionNotReady` if the TUI never came
    up, nothing having been written so the caller can resend, and lets the
    write through if the TUI is up but still busy. Returns the session as it
    was last read, or ``None`` when it could not be.
    """
    deadline = time.monotonic() + _READY_WAIT_S
    waited = False
    while True:
        try:
            info = get_session(port, session_id)
        except SessionHostError:
            return None
        state = _boot_state(info)
        if state == _BOOT_DONE:
            if waited:
                logger.info("✅ input: %s ready after waiting for its TUI", session_id[:8])
            return info
        if time.monotonic() >= deadline:
            if state == _BOOT_PAINTING:
                logger.info("ℹ️ input: %s still painting after %.0fs, sending", session_id[:8], _READY_WAIT_S)
                return info
            logger.warning(
                "⚠️ input: %s still has no TUI after %.0fs, nothing sent",
                session_id[:8], _READY_WAIT_S,
            )
            raise SessionNotReady(
                f"session {session_id} is still starting (its agent has not drawn "
                f"its input box) — NOT delivered, nothing was sent; retry shortly",
                status=409,
            )
        if not waited:
            logger.info("ℹ️ input: %s is still starting (%s), holding the write", session_id[:8], state)
            waited = True
        time.sleep(_READY_POLL_S)


def _refuse_if_awaiting_trust(session_id: str, info: Dict[str, Any]) -> None:
    """Raise :class:`FolderTrustPending` while a Copilot session shows its
    folder-trust dialog: it has its title and has stopped painting, so the
    readiness wait calls it ready, yet text typed there is lost."""
    prompt = folder_trust.pending_for_session(info)
    if prompt is None:
        return
    logger.warning(
        "⚠️ input: %s is waiting on the folder-trust prompt for %s, nothing sent",
        session_id[:8], folder_trust.folder_name(prompt.folder),
    )
    raise FolderTrustPending(session_id, prompt.folder)


def send_input(port: int, session_id: str, data: str, submit: bool = True) -> Dict[str, Any]:
    """Write ``data`` and, if ``submit``, submit it.

    A just-spawned Copilot session is waited for first (:func:`_await_tui_ready`):
    text written before it is ready is silently lost, and the host would
    still report it as written. One sitting on Copilot's folder-trust dialog
    is refused (:class:`FolderTrustPending`) for the same reason.

    One call, not two — the session-host now owns the whole framing +
    settle-then-submit sequence internally (``PtySession.submit_input``), so
    the caller no longer needs to send the text and the CR as two separate
    requests.

    The returned body carries the host's delivery verdict (``reason``,
    ``ingested``, ``submitted``, ``submit_confirmed``, ``submit_state``,
    ``delivered``, ``deferred``, ``queued`` —
    ``delivered`` is
    never true for a requested submit that was not sent — see
    ``docs/board.md``); a payload the terminal never echoed raises
    ``SessionHostError`` with a 502 rather than returning a success body.
    ``reason: "deferred"`` (the host answers 202) means the payload is in the
    composer and its submit is with a background watcher — the final verdict
    lands on the session's ``last_input``, not on this response. The status
    code itself is not surfaced here; the ``reason`` field is the contract.
    """
    info = _await_tui_ready(port, session_id)
    if info is not None:
        _refuse_if_awaiting_trust(session_id, info)
    return _request(
        "POST", port, f"/sessions/{session_id}/input",
        json={"data": data, "submit": submit}, timeout=_INPUT_TIMEOUT,
    )


def discard_pending_input(port: int, session_id: str, entry_id: str) -> Dict[str, Any]:
    """Discard one of a session's ``pending_input`` messages.

    The body's ``reason`` is the verdict on a success: ``discarded`` (the
    queue-edit keys went in) or ``discarding`` (the watcher clears the
    composer at its next safe moment). Already taken (404) and not possible
    now (409, the detail says why) raise ``SessionHostError``. The keys are
    pressed with pauses under the session's write lock, hence the input
    call's timeout.
    """
    return _request(
        "DELETE", port, f"/sessions/{session_id}/pending-input/{entry_id}",
        timeout=_INPUT_TIMEOUT,
    )


def stop(port: int, session_id: str, mode: str = "quit") -> Dict[str, Any]:
    return _request(
        "POST", port, f"/sessions/{session_id}/stop",
        json={"mode": mode}, timeout=_STOP_TIMEOUT,
    )


def rename(port: int, session_id: str, title: str) -> Dict[str, Any]:
    """Set (empty ``title`` clears) a manual title override."""
    return _request(
        "POST", port, f"/sessions/{session_id}/rename", json={"title": title}
    )


def upload_image(
    port: int,
    session_id: str,
    filename: str,
    content: bytes,
    content_type: str,
    inline: bool = False,
) -> Dict[str, Any]:
    """Upload an image into a session. With ``inline`` the session-host
    skips pasting the path into the PTY and just returns it."""
    return _request(
        "POST",
        port,
        f"/sessions/{session_id}/image",
        files={"file": (filename, content, content_type)},
        params={"inline": "1"} if inline else None,
    )


def supports_large_upload(port: int) -> Optional[bool]:
    """Whether the running session-host has the large-file route:
    None when it is unreachable, so the caller can tell "down" from
    "older than the feature" (it is excluded from the webapp's restart and
    may run an old build for days)."""
    body = identity(port)
    if body is None:
        return None
    return "large_upload" in (body.get("features") or [])


async def upload_large_file(
    port: int,
    session_id: str,
    name: str,
    chunks: AsyncIterator[bytes],
    max_bytes: int,
) -> Dict[str, Any]:
    """Stream a large file into a session, never holding it whole.

    Async and on its own short-lived client rather than the pooled
    requests session: the body is an async stream straight off the
    phone's request, and the pool's retry-once on a dropped connection would
    resend a half-consumed stream as if it were the whole file. Loopback
    only, so trust_env=False keeps any proxy variable out of the path.
    An exception raised by chunks propagates unchanged.
    """
    url = base_url(port) + f"/sessions/{session_id}/large-file"
    try:
        async with httpx.AsyncClient(timeout=_LARGE_UPLOAD_TIMEOUT, trust_env=False) as client:
            resp = await client.post(
                url,
                params={"name": name, "max_bytes": max_bytes},
                content=chunks,
                headers={"content-type": "application/octet-stream"},
            )
    except httpx.TransportError as exc:
        raise SessionHostError(f"session-host unreachable at {url} ({exc})", status=503) from exc
    if resp.status_code >= 400:
        raise SessionHostError(
            _loopback_http.detail(resp, "session-host"), status=resp.status_code
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise SessionHostError(f"session-host returned non-JSON ({exc})") from exc
