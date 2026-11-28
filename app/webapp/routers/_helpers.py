"""Cross-router helpers — no router imports another router; shared utility
lives here instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from concurrent.futures import Executor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import HTTPException, Request, WebSocket

from app.webapp.route_pools import AUDIT, OUTBOUND, SESSION_HOST, run_on
from src import audit, board, instance_role
from src import jobs as jobs_mod
from src.auth.webauthn_gate import WebAuthnGate
from src.clients.session import SessionHostError
from src.jobs.config import get_by_id, load_jobs
from src.launching import copilot_remote
from src.launching.presets import OPEN_MODEL_AGENTS, PRESET_CONTEXTS, efforts_for
from src.session_hosting.body_fields import (
    safe_int as safe_int,  # the routers import it from here
)
from src.session_view.copilot_host import copilot_signed_in_host
from src.session_view.transcript_locate import copilot_task_id
from src.webapp_config import (
    WebappConfig,
    append_auth_token,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def requested_launch_effort(
    body: Dict[str, Any], agent: str, model_value: str
) -> Optional[str]:
    """The body's per-launch ``effort`` (a launch preset's), ``None`` when absent.

    400 when it is not a string, when no per-launch model was sent for it to
    apply to, or when that model does not offer it; the message names the
    allowed values.
    """
    effort = body.get("effort")
    if effort is None or effort == "":
        return None
    if not isinstance(effort, str):
        raise HTTPException(status_code=400, detail="effort must be a string")
    effort = effort.strip().lower()
    offered = efforts_for(agent, model_value)
    if not model_value or effort not in offered:
        allowed = ", ".join(offered) if offered else "none"
        target = f"{agent}:{model_value}" if model_value else f"{agent} (no model sent)"
        raise HTTPException(
            status_code=400,
            detail=f"effort {effort!r} is not offered by {target}; allowed: {allowed}",
        )
    return effort


def requested_launch_context(body: Dict[str, Any], agent: str) -> Optional[str]:
    """The body's per-launch Copilot ``context`` tier (a launch preset's), ``None`` when absent.

    400 when it is not a string, when the agent takes no context tier, or when
    the tier is not one the CLI offers; the message names the allowed values.
    """
    context = body.get("context")
    if context is None or context == "":
        return None
    if not isinstance(context, str):
        raise HTTPException(status_code=400, detail="context must be a string")
    context = context.strip().lower()
    if agent not in OPEN_MODEL_AGENTS or context not in PRESET_CONTEXTS:
        allowed = ", ".join(PRESET_CONTEXTS) if agent in OPEN_MODEL_AGENTS else "none"
        raise HTTPException(
            status_code=400,
            detail=f"context {context!r} is not offered by {agent}; allowed: {allowed}",
        )
    return context


def known_run_dir(job_id: str, run_id: str) -> Optional[Path]:
    """A registered job's existing run dir, else ``None``.

    The job-exists check, join and ``is_dir`` every Jobs route needs before it
    acts on a ``{job_id}/{run_id}`` pair.
    """
    if get_by_id(load_jobs(), job_id) is None:
        return None
    run_dir = jobs_mod.runs_dir(job_id) / run_id
    return run_dir if run_dir.is_dir() else None


async def audit_off_loop(
    write: Callable[..., None], *args: Any, _pool: Optional[Executor] = None, **kwargs: Any
) -> None:
    """Run one ``src.audit`` write off the event loop.

    Every audit entry point does synchronous ``open``/``write``/``close``:
    ``session_log``/``session_input`` on ``webapp/sessions/<sid>.log``,
    ``audit_event`` on the cross-session log through a logging handler. The
    webapp runs a **single** uvicorn worker (``app/webapp/event_loop.py``), so
    a slow write — disk contention, an AV scanner walking that directory —
    freezes every other live session's in-flight WS output: the "terminal
    opens blank and never paints" class this guards against, same as the
    session-host's own handler.

    Every audit call site across every router goes through this one hop off
    the loop, rather than each router threading its own call — a helper
    copied per router would silently drift back toward the on-loop write
    this exists to prevent.

    Deliberately no try/except: ``audit.session_log``/``session_input``
    already swallow their own ``OSError``, and ``audit_event`` going bang is a
    real fault worth surfacing.

    ``_pool`` runs the write on that executor instead of the audit pool,
    for a route whose answer must not wait behind other routes' threads (a
    message send, on its own input pool).
    """
    await run_on(AUDIT if _pool is None else _pool, write, *args, **kwargs)


async def maybe_json(request: Request) -> Dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def cert_present() -> bool:
    return (
        (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem").exists()
        and (PROJECT_ROOT / "webapp" / "certificates" / "key.pem").exists()
    )


# (mtime, hostname-or-None) of the last classified cert.pem — the cert only
# changes on provision/renew, so one parse per change, not per PTY launch.
_TSNET_CACHE: Optional[tuple[float, Optional[str]]] = None


def tsnet_host_from_cert(cert_path: Optional[Path] = None) -> Optional[str]:
    """The .ts.net hostname the active cert is issued for, or None.

    Returns a hostname only for a genuine ``tailscale cert`` leaf — keyed on
    the ISSUER (Let's Encrypt), because a self-signed leaf also carries the
    ts.net name in its SAN and must keep routing mirrors over loopback
    (same discriminator as scripts/gen_tailscale_cert.py).
    """
    global _TSNET_CACHE
    path = cert_path or (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if cert_path is None and _TSNET_CACHE is not None and _TSNET_CACHE[0] == mtime:
        return _TSNET_CACHE[1]
    host: Optional[str] = None
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        cert = x509.load_pem_x509_certificate(path.read_bytes())
        issuer_orgs = [
            attr.value
            for attr in cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        ]
        if any("let's encrypt" in str(org).lower() for org in issuer_orgs):
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            for name in san.value.get_values_for_type(x509.DNSName):
                if ".ts.net" in name:
                    host = name
                    break
    except Exception:
        host = None
    if cert_path is None:
        _TSNET_CACHE = (mtime, host)
    return host


def mirror_url(request: Request, cfg: WebappConfig, sid: str) -> str:
    """The URL a launcher-spawned PC terminal window opens for ``sid``.

    Self-signed / no cert → loopback, whose auth bypass the mirror rides.
    With a Tailscale LE cert the loopback URL would hit a
    hostname-mismatch interstitial (the cert names only the ts.net host), so
    the mirror targets the ts.net URL instead and carries its credentials
    explicitly: ``?token=`` bootstraps the bearer (same mechanism as the
    tunnel URL) and ``?tt=`` a server-minted terminal token when the passkey
    gate is configured. Trust-equivalent to the loopback bypass — the window
    is spawned by this server, on this machine, for this user;
    the SPA strips both params from the visible URL on boot.
    """
    ts_host = tsnet_host_from_cert()
    if ts_host is None:
        scheme = "https" if cert_present() else "http"
        return f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
    url = append_auth_token(
        f"https://{ts_host}:{cfg.port}/?terminal={sid}", cfg.auth_token
    )
    gate = getattr(request.app.state, "webauthn_gate", None)
    if gate is not None and WebAuthnGate.configured(cfg):
        url += "&" + urlencode({"tt": gate.mint_local_token()})
    return url


# ------------------------------------------------- provider session links
#
# Claude Code's remote-control card prints an ``https://claude.ai/code/session_…``
# URL into the PTY. That link is authenticated by the Anthropic account and
# opens from any browser on any network, unlike this launcher's own
# ``?session=`` deep link, which is tailnet-gated by design. Both the Coding
# tab (``/api/claude-code/sessions``) and the Board (``/api/board``) hand it
# to the client as ``web_url``, so the resolution lives here rather than in
# either router.

_CLAUDE_WEB_URL_RE = re.compile(
    r"https://claude\.ai/code/session_"
    r"\s*([A-Za-z0-9](?:\s*[A-Za-z0-9]){23})(?![A-Za-z0-9])"
)
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SESSION_LINK_SCAN_BYTES = 512 * 1024

# A captured URL never changes for a session id, so memoize the hit: the
# Coding-tab poll and the Board poll both run on a 5s cadence and overlap
# while the Board tab is up (main.js — fetchSessions is not tab-gated), which
# without this would re-scan up to 1 MiB of every live transcript twice every
# five seconds. An empty result means the card has not been printed *yet*, so
# a miss is cached only against the transcript it was read from: its
# size and mtime, from one stat. An unchanged transcript is not reopened on
# the next poll (a long-lived session whose card scrolled out of both windows
# would re-read 1 MiB every tick, and each open is another chance at a
# multi-second stall); one that has grown is scanned again.
_PROVIDER_URL_CACHE: Dict[str, str] = {}
_PROVIDER_URL_MISSES: Dict[str, Tuple[int, int]] = {}
_PROVIDER_URL_CACHE_MAX = 512

# GitHub Copilot's enterprise web view addresses a task as
# ``https://<host>/copilot/tasks/<task id>``. Nothing is memoized here: the
# host and the task id are each re-read only when their own file changes.
_COPILOT_TASKS_PATH = "/copilot/tasks/"
_PUBLIC_GITHUB_HOST = "github.com"


def provider_web_url(agent: str, transcript: Path) -> str:
    """Return the provider-native web URL captured from a PTY transcript.

    Claude's remote-control card often wraps between ``session_`` and its
    24-character identifier, with cursor-control sequences around the second
    line. Read bounded head and tail windows, strip CSI, and join only the
    whitespace inside that known URL shape. Codex local sessions deliberately
    return no URL: its remote-control surface is not available on the web.
    """
    if agent != "claude":
        return ""
    try:
        with transcript.open("rb") as stream:
            head = stream.read(_SESSION_LINK_SCAN_BYTES)
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            tail = b""
            if size > _SESSION_LINK_SCAN_BYTES:
                stream.seek(max(0, size - _SESSION_LINK_SCAN_BYTES))
                tail = stream.read(_SESSION_LINK_SCAN_BYTES)
    except OSError as exc:
        logger.debug(f"provider link transcript read failed: {exc}")
        return ""

    text = _ANSI_CSI_RE.sub("", (head + b"\n" + tail).decode("utf-8", errors="replace"))
    match = _CLAUDE_WEB_URL_RE.search(text)
    if not match:
        return ""
    session_token = re.sub(r"\s+", "", match.group(1))
    return f"https://claude.ai/code/session_{session_token}"


def copilot_web_base(override: str) -> str:
    """The URL a Copilot task id is appended to, or ``""`` for none.

    An ``http(s)`` override wins; otherwise the enterprise host the Copilot
    CLI is signed in to. github.com derives nothing: the ``/copilot/tasks/``
    template is the enterprise view, so such an account gets a link only
    through the override. The host is never a repo value.
    """
    override = (override or "").strip()
    if override.startswith(("https://", "http://")):
        return override
    host = copilot_signed_in_host()
    if not host or host == _PUBLIC_GITHUB_HOST:
        return ""
    return f"https://{host}{_COPILOT_TASKS_PATH}"


def attach_provider_web_urls(
    sessions: List[Dict[str, Any]],
    cfg: Optional[WebappConfig] = None,
    state_rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """Enrich live full-control rows without requiring a session-host restart.

    Every row gets a ``web_url`` key so the client never has to distinguish
    "no link" from "field absent"; only ``kind == "pty"`` rows are resolved:
    Claude's from its captured remote-control card, Copilot's from the link
    base (:func:`copilot_web_base`) plus its task id. The Copilot session is
    named by the state row it claims (a Board card carries it as
    ``state_sid``, the Coding list passes ``state_rows``), else by the launch
    match. With no base, no Copilot folder is read.
    Blocking file IO — callers run it off the event loop.
    """
    override = cfg.copilot_web_url_prefix if cfg is not None else ""
    copilot_base: Optional[str] = None
    for session in sessions:
        session["web_url"] = ""
        if session.get("kind") != "pty":
            continue
        sid = str(session.get("session_id") or "")
        if session.get("agent") == "copilot":
            if copilot_base is None:
                copilot_base = copilot_web_base(override).rstrip("/")
            if not copilot_base:
                continue
            row_sid = session.get("state_sid")
            if not row_sid and state_rows:
                row_sid = board.state_sid_for_session(sessions, state_rows, sid)
            task_id = copilot_task_id(session, row_sid)
            if task_id:
                session["web_url"] = f"{copilot_base}/{task_id}"
            continue
        cached = _PROVIDER_URL_CACHE.get(sid)
        if cached:
            session["web_url"] = cached
            continue
        agent = str(session.get("agent") or "")
        transcript = audit.transcript_path(sid)
        identity = _transcript_identity(transcript) if agent == "claude" and sid else None
        if identity is not None and _PROVIDER_URL_MISSES.get(sid) == identity:
            continue
        url = provider_web_url(agent, transcript)
        session["web_url"] = url
        if url and sid:
            _remember(_PROVIDER_URL_CACHE, sid, url)
            _PROVIDER_URL_MISSES.pop(sid, None)
        elif identity is not None:
            _remember(_PROVIDER_URL_MISSES, sid, identity)


def _transcript_identity(transcript: Path) -> Optional[Tuple[int, int]]:
    """``(size, mtime_ns)`` of a PTY transcript; ``None`` when it can't be
    stat'ed, which caches nothing, so the next poll tries again."""
    try:
        st = transcript.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _remember(cache: Dict[str, Any], sid: str, value: Any) -> None:
    if len(cache) >= _PROVIDER_URL_CACHE_MAX:
        # Dead session ids accumulate over a long-lived tray. Drop the lot
        # rather than track recency: the cost of rebuilding is one extra scan
        # per live session, once.
        cache.clear()
    cache[sid] = value


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def launch_mirror_enabled(cfg: WebappConfig) -> bool:
    """Whether a *launch* may open a PC mirror window at all.

    One source of truth for every launch path: window mirroring must be
    available (``claude_show_local_window``) and the owner must have asked for
    windows (Settings' "Open sessions in a new window",
    ``open_sessions_in_window``). Off, a session renders in the page whoever
    started it and from whatever surface. The row menu's explicit "Open in new
    window" does not come through here: it posts to ``/mirror``, which needs
    only ``claude_show_local_window``.
    """
    return bool(cfg.claude_show_local_window and cfg.open_sessions_in_window)


def should_mirror_to_pc(
    show_local_window: bool, request: Request, body: Dict[str, Any]
) -> bool:
    """Whether a PTY launch should open the PC mirror window.

    The default is to mirror; only a caller that explicitly says it will
    render the session itself skips it:

    * **Phone** — non-loopback and no ``desktop`` flag: the PC has no window
      of its own, so mirror to one.
    * **Desktop browser** — ``desktop: true`` in the launch body: mirror to
      a dedicated, independently-closable Edge window rather
      than rendering the terminal inside the user's own browser. Rendering
      the terminal in-page here too would look redundant but is not: that
      in-page render is the very thing that lets Stop & Close tear down the
      controlling Chrome window, so the dedicated mirror window is required.
      The flag (set client-side by ``isDesktopClient``) is what distinguishes
      a desktop from a phone regardless of loopback vs tunnel.
    * **Genuine in-page loopback browser** — ``in_page: true`` in the launch
      body: the SPA itself sets this whenever it's about to
      render the session in-page (``launch-size.js``'s ``applyLaunchSizePayload`` non-desktop
      branch — a phone always takes this branch too, but the non-loopback
      check above already mirrors it before this is ever consulted). An
      *explicit* signal, not an inference from "loopback and no desktop
      flag" — that inference alone cannot tell a genuine in-page browser
      apart from every other non-browser loopback API caller (a script, the
      fleet chief orchestrator dispatching over ``127.0.0.1``), which has no
      page to render into and so must still get a window. Any other
      loopback caller — including one that sends neither
      flag — mirrors by default.

    A disposable e2e / verify-before-ship instance (``LAUNCHER_SESSION_HOST_PORT``
    set, which only autoboot does) never mirrors, whatever the caller sends —
    the rule ``/api/claude-code/sessions/<sid>/mirror`` and the orphan-mirror
    sweep both follow. Without it, every gate run's real-agent
    launch would leave a real Edge window on the user's desktop: the
    window loads the throwaway instance, can't authenticate, never reaches
    ``announceMirrorWindow`` to set its ``app-launcher-lite-mirror-<sid>`` title
    marker, and so is invisible to both the Stop & Close and orphan sweeps.
    """
    # Imported here to avoid a module-load cycle (middleware imports nothing
    # from the routers package, but keep the dependency edge one-directional).
    from src.middleware.trust import LOOPBACK_HOSTS

    if not show_local_window:
        return False
    if instance_role.is_disposable():
        return False
    if bool(body.get("desktop")):
        return True
    if client_ip(request) not in LOOPBACK_HOSTS:
        return True
    return not bool(body.get("in_page"))


async def spawn_session_or_400(
    spawn_fn: Callable[..., Dict[str, Any]], /, *args: Any, **kwargs: Any
) -> Dict[str, Any]:
    """Run a session spawn off the event loop, mapping its failures to HTTP.

    ``SessionHostError`` carries the session-host's own status through
    (``exc.status``) so a 409 "already running" doesn't flatten into a 400; an
    ``OSError`` (an unreadable project dir, a missing executable) is a bad
    request by the time it reaches here.

    ``spawn_fn`` is passed in rather than imported here so the per-router
    ``spawn_claude_session`` monkeypatches in tests still apply when the spawn
    runs on the caller's behalf.
    """
    try:
        return await run_on(SESSION_HOST, spawn_fn, *args, **kwargs)
    except SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def ensure_copilot_remote(cfg: WebappConfig, agent: str) -> None:
    """Make Copilot's remote-session settings match Settings before a Copilot launch.

    Every Copilot launch route calls this ahead of its spawn
    (:func:`spawn_launcher_session` does it itself); any other agent is a
    no-op. It runs off the event loop and never fails the launch: see
    :func:`src.launching.copilot_remote.apply`.
    """
    if agent == "copilot":
        await run_on(OUTBOUND, copilot_remote.apply, cfg.copilot_remote_session)


async def spawn_launcher_session(
    spawn_fn: Callable[..., Dict[str, Any]],
    cfg: WebappConfig,
    *,
    project_dir: Path,
    name: str,
    flags: str,
    agent: str,
    rows: int,
    cols: int,
    kind: str = "pty",
    label: str = "",
) -> Tuple[Dict[str, Any], str]:
    """Spawn one launcher-owned session and return ``(session, sid)``.

    Adds the two ``cfg``-derived spawn arguments (``session_host_port`` and
    ``history_lines``) and applies Copilot's remote-session setting first;
    ``label`` is sent only when set.

    It stops before :func:`audit_session_start_and_maybe_mirror` on purpose: a
    caller may have order-dependent work between the spawn and that tail, such
    as typing into the new session.

    ``spawn_fn`` is passed in for the same reason as in
    :func:`spawn_session_or_400`.
    """
    extra: Dict[str, Any] = {"history_lines": cfg.terminal_history_lines}
    if label:
        extra["label"] = label
    await ensure_copilot_remote(cfg, agent)
    session = await spawn_session_or_400(
        spawn_fn,
        project_dir,
        name,
        flags,
        cfg.session_host_port,
        kind,
        agent,
        rows,
        cols,
        **extra,
    )
    return session, str(session.get("session_id") or "")


async def audit_session_start_and_maybe_mirror(
    cfg: WebappConfig,
    request: Optional[Request],
    body: Dict[str, Any],
    *,
    sid: str,
    agent: str,
    name: str,
    project: str,
    audit_mod: Any,
    mirror_fn: Callable[[str, str], Any],
    resume: Optional[bool] = None,
    skill: Optional[str] = None,
    kind: str = "pty",
    resume_sid: Optional[str] = None,
    brief_chars: Optional[int] = None,
    mirror: bool = True,
) -> None:
    """Audit a freshly spawned session, then mirror it to a PC window if due.

    ``kind="remote"`` audits as ``remote_launch`` instead of ``session_start``
    and is never mirrored: a detached session has no window. ``mirror=False``
    suppresses the window for any kind. ``request`` is ``None`` for a start no
    client asked for, which is audited as ``startup`` and not mirrored.
    ``resume``, ``resume_sid``, ``skill`` and ``brief_chars`` left ``None``
    are dropped from the audit line.

    ``audit_mod`` / ``mirror_fn`` must be the *caller's own* module-level
    ``audit`` / ``open_local_terminal_window`` references, not this module's,
    so the per-router monkeypatches in tests (no real audit log, no real
    window) still apply when this runs on the caller's behalf.
    """
    # Both writes go off the event loop: this runs on every launch, so a slow
    # audit write here would freeze the single-worker loop each time.
    await audit_off_loop(
        audit_mod.audit_event,
        "remote_launch" if kind == "remote" else "session_start",
        session=sid,
        agent=agent,
        skill=skill,
        name=name,
        project=project,
        resume=resume,
        # Which conversation was reattached — "" for a fresh launch
        # or the native picker, where no id was chosen up front. None for a
        # caller that has no such concept, and dropped from the line.
        resume_sid=resume_sid,
        # A Board issue-start dispatch brief: its size only, never
        # its content. None (no brief) is dropped from the line.
        brief_chars=brief_chars,
        # No request: no client asked for this start.
        client=client_ip(request) if request is not None else "startup",
    )
    await audit_off_loop(
        audit_mod.session_log,
        sid, "start", agent=agent, skill=skill, name=name, project=project,
    )
    # Mirror the session into a dedicated interactive terminal window on the
    # PC — the default for every caller; only
    # an explicit in-page loopback browser skips it (see should_mirror_to_pc),
    # and a caller passing ``mirror=False`` — the fleet chief's own Board
    # dispatch, which runs unwatched until the owner opens it.
    # mirror_url picks loopback (auth-bypass) or the ts.net URL with explicit
    # credentials, keyed on the active cert.
    if mirror and request is not None and kind == "pty" and should_mirror_to_pc(
        launch_mirror_enabled(cfg), request, body
    ):
        # Pass sid so launcher tracks the mirror window's HWND for Stop &
        # Close to dismiss it later.
        asyncio.create_task(
            run_on(OUTBOUND, mirror_fn, mirror_url(request, cfg, sid), sid)
        )


def client_ip_ws(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "?"
