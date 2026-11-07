"""Password login → bearer token swap.

The bearer token in `webapp_config.json` is never sent to the client until
a correct password is presented. Failed attempts and successful logins
both land in `webapp/auth.log` (separate from the main launcher log so a
phone-side review is easy).

Repeated wrong passwords from one client are slowed by the vendored
`src.login_lockout.LoginLockout` (a `429` with `Retry-After`) and raise one
Telegram alert; the PC itself is never locked out. The lock-out sits in front
of the password check and changes none of it.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from app.webapp.middleware import LOOPBACK_HOSTS, via_cloudflare
from app.webapp.routers._helpers import PROJECT_ROOT, maybe_json
from src.api_tokens import constant_time_equals
from src.log_files import ensure_file_log_handler
from src.login_lockout import LockoutAlert, LoginLockout, retry_after_header
from src.notify import Notifier, NotifierError, TelegramConfig, build_notifier
from src.webapp_config import WebappConfig

auth_logger = logging.getLogger("launcher.auth")
_AUTH_LOG_PATH = PROJECT_ROOT / "webapp" / "auth.log"

APP_NAME = "App Launcher Lite"


def ensure_log_handler() -> None:
    """Attach the auth.log file handler exactly once. Idempotent — safe to
    call from `create_app()` on every boot."""
    ensure_file_log_handler(auth_logger, _AUTH_LOG_PATH, logging.INFO)


def resolve_client(request: Request) -> Tuple[str, bool]:
    """``(client address, is the PC itself)`` for a login request.

    cloudflared dials the webapp over loopback, so a loopback socket alone
    proves nothing: with a Cloudflare edge header the caller is a public
    visitor, keyed on the edge's ``cf-connecting-ip`` (read only from a
    loopback socket, where only the tunnel can have set it, and only when it
    parses as an address). The PC itself is a loopback socket with no edge
    header, the same trust check as ``ws_terminal_gate``.
    """
    host = request.client.host if request.client else "?"
    if host not in LOOPBACK_HOSTS:
        return host, False
    if not via_cloudflare(request.headers):
        return host, True
    edge = request.headers.get("cf-connecting-ip", "").strip()
    try:
        return str(ipaddress.ip_address(edge)), False
    except ValueError:
        return host, False


def _build_alert_notifier(cfg: WebappConfig) -> Optional[Notifier]:
    """Telegram notifier for the guessing alert, or ``None`` when no bot token
    or chat is configured. The attention chat wins over the per-job chat."""
    chat_id = cfg.telegram_alert_chat_id or cfg.telegram_chat_id
    if not (cfg.telegram_bot_token and chat_id):
        return None
    return build_notifier("telegram", TelegramConfig(cfg.telegram_bot_token, chat_id))


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True, name="login-alert").start()


def make_login_alert(
    get_config: Callable[[], WebappConfig],
    *,
    build: Callable[[WebappConfig], Optional[Notifier]] = _build_alert_notifier,
    spawn: Callable[[Callable[[], None]], None] = _start_thread,
) -> Callable[[LockoutAlert], None]:
    """``on_alert`` for the lock-out: send one Telegram message from a thread.

    The login request returns at once; the message names the app, the client
    address and the failure count, never a password or the token. The config
    is read when the alert fires so an edited chat id applies without a restart.
    """

    def alert_owner(alert: LockoutAlert) -> None:
        notifier = build(get_config())
        if notifier is None:
            auth_logger.info("ℹ️ Login alert not sent: no Telegram bot/chat configured")
            return
        held = f" (+{alert.suppressed} more held back)" if alert.suppressed else ""
        text = (
            f"🚨 {APP_NAME}: {alert.failures} wrong passwords in a row "
            f"from {alert.key}{held}"
        )

        def send() -> None:
            try:
                notifier.send_text(text)
            except NotifierError as exc:
                auth_logger.warning(f"⚠️ Login alert not delivered: {type(exc).__name__}")

        spawn(send)

    return alert_owner


router = APIRouter()


@router.post("/api/login")
async def login(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    lockout: LoginLockout = request.app.state.login_lockout
    client_host, is_pc = resolve_client(request)
    wait = 0.0 if is_pc else lockout.retry_after(client_host)
    if wait > 0:
        auth_logger.warning(f"🔒 Login from {client_host} refused: locked out")
        raise HTTPException(
            status_code=429,
            detail="too many attempts",
            headers=retry_after_header(wait),
        )
    if not cfg.auth_password:
        auth_logger.info(
            f"⚠️  Login attempt from {client_host} but no auth_password configured"
        )
        raise HTTPException(
            status_code=503, detail="password auth not configured"
        )
    if not cfg.auth_token:
        auth_logger.info(
            f"⚠️  Login attempt from {client_host} but no auth_token configured"
        )
        raise HTTPException(
            status_code=503, detail="bearer token not configured"
        )
    body = await maybe_json(request)
    presented = str(body.get("password") or "")
    if not presented or not constant_time_equals(presented, cfg.auth_password):
        if not is_pc:
            lockout.record_failure(client_host)
        auth_logger.warning(
            f"🚨 Failed password attempt from {client_host} "
            f"(presented: {len(presented)} chars)"
        )
        raise HTTPException(status_code=401, detail="bad password")
    lockout.reset(client_host)
    auth_logger.info(f"🔓 Password login from {client_host}")
    return {"token": cfg.auth_token}
