"""Reach app-launcher-lite's webapp over whichever scheme its port serves.

Lite's Settings > Connections > Serve over picks HTTP, HTTPS, both (HTTPS on the
webapp port, plain HTTP on a second one) or auto (HTTPS when a certificate pair
exists). The chief helpers only know the port, so the scheme in a base URL is a
hint: `resolve` asks the port itself, once per process, and every call after
that uses the answer. An HTTP request sent to a TLS listener comes back as a
dropped connection, never an HTTP answer, so a guessed scheme cannot be allowed
to reach a caller as "the launcher is down".

The probe is one plaintext `HEAD` on loopback: a reply starting `HTTP/` is a
plain HTTP server, and anything else (a TLS alert, a closed connection) is a TLS
listener. A plaintext probe always gets an answer from either kind, whereas a
TLS ClientHello can sit unanswered in a plain server that is still waiting for
a header line. Certificate checks are off only because the host is loopback,
which `request` asserts before anything is sent.

The session-host is plain loopback HTTP on its own port and never serves TLS, so
its port is never probed or rewritten.

stdlib only: this runs from the installed Copilot folder, outside the app's venv.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

import lite_ports

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PROBE_TIMEOUT = 2.0
_PROBE = b"HEAD / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"

# (host, port) -> the scheme the port answered in, for this process's life.
# `forget` drops an entry the moment a call proves it wrong, so a `serve_over`
# change is picked up by the next call instead of failing every later one.
_schemes: Dict[Tuple[str, int], str] = {}


class WrongSchemeError(urllib.error.URLError):
    """The port answered in the other protocol: the request was never handled as HTTP."""


def _split(base_url: str) -> Tuple[urllib.parse.SplitResult, str, int]:
    parts = urllib.parse.urlsplit(base_url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts, host, port


def is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname
    return host is not None and host.lower() in LOOPBACK_HOSTS


def probe_scheme(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> Optional[str]:
    """`"http"` or `"https"` for what `host:port` serves, or None when it stayed silent.

    A refused connection raises `URLError`, as the real request would: nothing is
    listening, and a second connect to learn that again costs Windows ~2 s.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except ConnectionRefusedError as exc:
        raise urllib.error.URLError(exc) from exc
    except OSError:
        return None
    with sock:
        try:
            sock.sendall(_PROBE)
            head = b""
            while len(head) < 5:
                chunk = sock.recv(5 - len(head))
                if not chunk:
                    break
                head += chunk
        except (ConnectionResetError, ConnectionAbortedError):
            return "https"
        except OSError:
            return None
    return "http" if head == b"HTTP/" else "https"


def resolve(base_url: str) -> str:
    """`base_url` with the scheme its loopback port actually serves.

    A non-loopback URL, the session-host's port and a port that stays silent
    come back unchanged: the request then fails or succeeds on its own terms,
    reported by the caller as that state. A refused port raises `URLError`.
    """
    parts, host, port = _split(base_url)
    if host not in LOOPBACK_HOSTS or parts.scheme not in ("http", "https"):
        return base_url
    if port == lite_ports.session_host_port():
        return base_url
    scheme = _schemes.get((host, port))
    if scheme is None:
        scheme = probe_scheme(parts.hostname or host, port)
        if scheme is None:
            return base_url
        _schemes[(host, port)] = scheme
    return parts._replace(scheme=scheme).geturl()


def forget(base_url: str) -> None:
    _, host, port = _split(base_url)
    _schemes.pop((host, port), None)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _open(url: str, method: str, data: Optional[bytes], timeout: float) -> Any:
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 -- loopback asserted by the caller
    context = _ssl_context() if url.startswith("https://") else None
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:  # noqa: S310 -- loopback asserted by the caller
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _handshake_failed(exc: BaseException) -> bool:
    """True when a TLS handshake failed: the request bytes were never sent."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, ssl.SSLError)


def _dropped(exc: BaseException) -> bool:
    """A connection dropped with no HTTP answer: what a TLS listener does to plain HTTP."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (http.client.RemoteDisconnected, http.client.BadStatusLine,
                               ConnectionResetError, ConnectionAbortedError))


def request(base_url: str, path: str, method: str = "GET", body: Optional[dict] = None,
            timeout: float = 10.0) -> Any:
    """One JSON call to the loopback webapp at `base_url`, over the scheme its port serves.

    When the port turns out to serve the other scheme (Lite restarted with another
    `serve_over`), the scheme is probed again. A failed TLS handshake sent nothing,
    and a read has no side effect, so either is retried once over the new scheme. A
    write dropped over plain HTTP is not resent: it raises `WrongSchemeError` and
    the next call uses the new scheme. An HTTP status is the launcher answering,
    so it is raised as it is and never retried here.
    """
    if not is_loopback(base_url):
        raise ValueError(f"refusing a non-loopback launcher host {urllib.parse.urlsplit(base_url).hostname!r}")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    first = resolve(base_url)
    try:
        return _open(first.rstrip("/") + path, method, data, timeout)
    except urllib.error.HTTPError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        if not (_handshake_failed(exc) or _dropped(exc)):
            raise
        forget(base_url)
        second = resolve(base_url)
        if urllib.parse.urlsplit(second).scheme == urllib.parse.urlsplit(first).scheme:
            raise
        if method == "GET" or _handshake_failed(exc):
            return _open(second.rstrip("/") + path, method, data, timeout)
        raise WrongSchemeError(
            f"the launcher now serves {urllib.parse.urlsplit(second).scheme}; "
            f"{method} {path} was dropped over {urllib.parse.urlsplit(first).scheme} and not resent"
        ) from exc
