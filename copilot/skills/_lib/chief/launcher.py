"""Every read and write the chief makes against the app-launcher-lite webapp,
plus the one `glab issue view` per issue ref.

Lite: a copy of fleet-config's `chief/launcher.py` with three seams changed --
`DEFAULT_BASE_URL` is lite's webapp (plain HTTP on the port in its config.json), `fetch_issue_state`
reads GitLab through `glab` and normalises to the `gh` shape the ported `rails`
and `issues` read, and the Codex model catalog read is gone (Copilot is the only
agent). Every endpoint path is the same on lite.

All HTTP goes through `_request`, which calls `assert_loopback` first: the tool
can never address anything but this PC's launcher, even with a `--base-url`
override. stdlib `urllib` only, never a `curl` subprocess. A transport failure
is reported as its own state by each wrapper, never folded into a verdict.
"""

from __future__ import annotations

import json
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gitlab_namespace
from no_window import NO_WINDOW

from . import rails

DEFAULT_BASE_URL = gitlab_namespace.DEFAULT_BASE_URL
# Sibling checkouts live next to this repo (`<parent>/<repo>`), as app-launcher-lite assumes.
REPOS_ROOT = Path(__file__).resolve().parents[4]

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Default per-request budget; the `UNCONFIRMED=` line names it.
REQUEST_TIMEOUT = 10.0

# `/api/jobs` shells out to schtasks per job: ~4 s cold, well under 1 s warm.
JOBS_TIMEOUT = 30.0

# ---- loopback guard (pure) -------------------------------------------------

def assert_loopback(url: str) -> None:
    """Raise if `url`'s host is not a loopback address.

    The single choke point every HTTP call routes through — `--base-url`
    stays overridable (tests point it at a local fixture server) but can
    never point this tool at a non-loopback host, so it can never become a
    remote-control surface.
    """
    host = urllib.parse.urlsplit(url).hostname
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        raise ValueError(f"chief_ops refuses a non-loopback host: {host!r}")


def is_timeout(exc: BaseException) -> bool:
    """True for a read timeout (raw `TimeoutError`) or a connect timeout
    (`URLError` wrapping one)."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    return isinstance(exc, TimeoutError)


def transport_failure(exc: BaseException) -> Optional[Tuple[str, int]]:
    """`(stdout line, exit code)` for a launcher call that got no answer, or
    None for anything else (an HTTP status, a bad body) so it keeps its own
    handling.

    Two distinct states, never folded together: `UNCONFIRMED` -- the launcher
    stayed silent past the budget, so a write may still have landed -- and
    `UNREACHABLE` -- the connection itself failed, so nothing was sent.
    """
    if is_timeout(exc):
        return f"UNCONFIRMED=timeout after {REQUEST_TIMEOUT:g}s", 1
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        return f"UNREACHABLE={exc.reason}", 2
    return None


# ---- launcher reads behind one command each ---------------------------------

def resolve_sid_via_board(base_url: str, sid: str) -> Tuple[Optional[str], Optional[str]]:
    """`resolve_session_id` against a freshly-fetched board.

    Every operator-facing command that takes a `sid` has to do this, because
    the only place an operator *gets* a sid is `board`/`sessions`, and both
    print the 8-char prefix (`:498`, `:523`) — never the full 32-char id.
    `exchange` resolved it; `say` and `stop` did not, so `say <prefix>` POSTed
    to a session id that does not exist and `say --verify` then read the
    resulting empty card as **pending** — "delivery likely, unconfirmed" for a
    steer that was never accepted at all.

    A board that cannot be read is not evidence that the prefix is wrong, so
    the id passes through unchanged and the real endpoint gets to answer —
    the same never-invent-a-verdict rule `resolve_session_id`'s no-match branch
    already follows.
    """
    try:
        board = _request(base_url, "/api/board")
    except (urllib.error.URLError, ValueError):
        return sid, None
    return rails.resolve_session_id(sid, board.get("columns") or {})


def fetch_running_fleet_jobs(base_url: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """(running fleet-wide job ids, None) or (None, why unreadable).

    Never raises: a jobs API that can't be read is its own `unknown` state,
    never folded into "none running".
    """
    try:
        return rails.running_fleet_jobs(_request(base_url, "/api/jobs", timeout=JOBS_TIMEOUT)), None
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def fetch_chief_settings(base_url: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """The launcher's `{"auto_compact_threshold": int}` -> `(settings, "")`,
    or `(None, why)` -- a launcher without the endpoint (404) is not the same
    fact as one that could not be reached."""
    try:
        result = _request(base_url, "/api/board/chief-settings")
    except urllib.error.HTTPError as exc:
        return None, f"launcher has no chief-settings endpoint (HTTP {exc.code})"
    except (urllib.error.URLError, ValueError):
        return None, "launcher unreachable"
    return (result, "") if isinstance(result, dict) else (None, "chief-settings reply was not an object")


# ---- the transport and the session-input wrappers ---------------------------

def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(
    base_url: str,
    path: str,
    method: str = "GET",
    body: Optional[dict] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + path
    assert_loopback(url)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    context = _ssl_context() if url.startswith("https://") else None
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


# glab spawns its own API calls; one issue view is well under this on a working network.
GLAB_TIMEOUT = 30.0


def run_glab(args: List[str], timeout: float = GLAB_TIMEOUT) -> subprocess.CompletedProcess:
    """`glab <args>`, UTF-8 decoded, no console window, stdin closed.

    Raises `FileNotFoundError` when glab is not installed: the callers report
    that as its own state, never as an empty answer.
    """
    return subprocess.run(  # noqa: S603 -- fixed executable, argv built by the callers
        ["glab", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
    )


def normalise_glab_issue(data: Dict[str, Any], repo: str) -> Dict[str, Any]:
    """`glab issue view --output json` as the `gh issue view --json` row the readers expect."""
    state = str(data.get("state") or "")
    return {
        "repo": repo,
        "number": data.get("iid"),
        "title": data.get("title"),
        "state": {"opened": "OPEN", "closed": "CLOSED"}.get(state, state.upper()),
        "labels": [{"name": str(name)} for name in data.get("labels") or []],
        "updatedAt": data.get("updated_at"),
    }


OWNER_UNKNOWN = "OWNER=unknown"


def resolve_owner(repo: str, owner: Optional[str] = None) -> Tuple[Optional[str], str]:
    """`(namespace, "")` for `repo`: `owner` when given, else the launcher's `gitlab_group`,
    else the sibling checkout's `origin` namespace; `(None, why)` when none answers."""
    if owner:
        return owner, ""
    return gitlab_namespace.resolve(REPOS_ROOT / repo)


def fetch_issue_state(repo: str, number: int, owner: Optional[str] = None) -> Dict[str, Any]:
    """One `glab issue view` per ref. A glab failure (not installed, not signed
    in, no such issue) becomes an `error` field rather than raising, so one bad
    ref never aborts the rest of a multi-repo table. A namespace nobody
    configured is its own `OWNER=unknown` error, never a guessed 404."""
    owner, why = resolve_owner(repo, owner)
    if owner is None:
        return {"repo": repo, "number": number, "error": f"{OWNER_UNKNOWN} ({why})"}
    try:
        res = run_glab(["issue", "view", str(number), "--repo", f"{owner}/{repo}", "--output", "json"])
    except FileNotFoundError:
        return {"repo": repo, "number": number, "error": "glab not installed"}
    except subprocess.TimeoutExpired:
        return {"repo": repo, "number": number, "error": f"glab timed out after {GLAB_TIMEOUT:g}s"}
    if res.returncode != 0:
        return {"repo": repo, "number": number, "error": res.stderr.strip() or "glab error"}
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return {"repo": repo, "number": number, "error": "unparseable glab output"}
    if not isinstance(data, dict):
        return {"repo": repo, "number": number, "error": "glab output was not an object"}
    return normalise_glab_issue(data, repo)


def fetch_exchange_marker(base_url: str, sid: str) -> Dict[str, Any]:
    """`{"available": bool, "timestamp": Any}` for `sid`'s current exchange.

    A transport error or an unparseable response both collapse to
    `available: False` — the same "can't tell" outcome `classify_exchange_marker`
    treats as pending, never as delivered. The read-path itself has known
    defects (another session's content, raw frames,
    `capture_unparseable`); this wrapper doesn't try to detect those, it just
    refuses to promote an unreadable read into a delivery signal.
    """
    try:
        result = _request(base_url, f"/api/board/sessions/{sid}/exchange")
    except (urllib.error.URLError, ValueError):
        return {"available": False, "timestamp": None}
    if not result.get("available"):
        return {"available": False, "timestamp": None}
    assistant = result.get("assistant") or {}
    return {"available": True, "timestamp": assistant.get("timestamp")}


def post_session_input(base_url: str, sid: str, text: str) -> Dict[str, Any]:
    """POST one steer into `sid`'s terminal. **Exactly one POST, always** —
    this helper never retries, and no caller may call it twice for the same
    steer (a resent `/issue-finish` can double-execute a merge).

    Returns `{"ok": bool, "reason": str|None, "http_status": int|None,
    "error": str|None}`.

    The session-host answers a *written but never echoed* payload with HTTP
    502, and an already-exited session with 409.
    Both used to escape `urllib` as an unhandled `HTTPError` and crash the
    tool with a traceback — so the one case where the API states outright
    "this was NOT delivered" was the single input the verifier could not
    process, while every verdict it *did* report rested on inference. Caught
    here, those become a `reason` the classifier can act on.

    Success bodies are returned as-is; `reason` (not the HTTP status) is the
    documented contract for `deferred`/202, per app-launcher's
    `src/session_client.py:159-163`.
    """
    try:
        result = _request(
            base_url, f"/api/claude-code/sessions/{sid}/input",
            method="POST", body={"data": text, "submit": True},
        )
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — a body we cannot read must not mask the status
            detail = ""
        # 502 is the host's "written, never echoed, no CR sent" — map it onto
        # the same reason vocabulary the body would have carried, so the
        # classifier has one language for authoritative negatives.
        reason = "not_ingested" if exc.code == 502 else "dropped" if exc.code == 409 else None
        return {
            "ok": False, "reason": reason, "http_status": exc.code,
            "error": f"HTTP {exc.code}: {detail.strip()[:300] or exc.reason}",
        }
    except (urllib.error.URLError, ValueError) as exc:
        # Could not reach the endpoint at all — not evidence either way.
        return {"ok": False, "reason": None, "http_status": None, "error": str(exc)[:300]}
    if not isinstance(result, dict):
        return {"ok": True, "reason": None, "http_status": None, "error": None}
    return {"ok": True, "reason": result.get("reason"), "http_status": None, "error": None}


def fetch_session_card(base_url: str, sid: str) -> Dict[str, Any]:
    """The session-host's own record for `sid` from the sessions list, or
    `{}` when it cannot be read. Source of `last_output_at` and — on hosts
    new enough to report `last_input`.

    An older host simply omits `last_input`; that is reported as unknown
    rather than guessed at, which is why the callers below print `unknown`
    instead of inventing a verdict.
    """
    try:
        payload = _request(base_url, "/api/claude-code/sessions")
    except (urllib.error.URLError, ValueError):
        return {}
    sessions = payload.get("sessions") if isinstance(payload, dict) else payload
    for card in sessions or []:
        if isinstance(card, dict) and str(card.get("session_id") or "") == sid:
            return card
    return {}


def _last_input(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """`card`'s `last_input` record, or None when absent or malformed."""
    record = card.get("last_input")
    return record if isinstance(record, dict) else None
