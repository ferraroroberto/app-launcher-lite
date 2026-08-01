"""Launcher config read/patch + machine status readout.

`/api/status` exposes the tunnel URL, TLS cert presence, and a
terminal-reachability hint so the SPA can explain up front when the
live terminal won't work on the current connection.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from src import boot_autostart
from src.launch_flags import build_copilot_flags
from src.webapp_config import (
    MAX_TERMINAL_HISTORY_LINES,
    MIN_TERMINAL_HISTORY_LINES,
    WebappConfig,
    update_webapp_config,
)

from app.webapp.middleware import terminal_reachability
from app.webapp.routers._helpers import (
    PROJECT_ROOT,
    cert_present,
    copilot_flags_payload,
    maybe_json,
)

router = APIRouter()


@router.get("/api/config")
async def get_config(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return {
        "host": cfg.host,
        "port": cfg.port,
        "projects_dir": cfg.projects_dir,
        "projects_ignore": cfg.projects_ignore,
        # Coding-row buttons the user hid (issue #666) — agent ids plus the
        # pseudo-id `github`. The SPA filters the row strip on this.
        "coding_hidden_agents": cfg.coding_hidden_agents,
        "apps_scan_root": cfg.apps_scan_root,
        "team_os_dir": cfg.team_os_dir,
        "terminal_history_lines": cfg.terminal_history_lines,
        "terminal_history_lines_min": MIN_TERMINAL_HISTORY_LINES,
        "terminal_history_lines_max": MAX_TERMINAL_HISTORY_LINES,
        "copilot": copilot_flags_payload(cfg),
        "auth_password_set": bool(cfg.auth_password),
        # Boot-autostart toggle (issue #456): live-queried, not stored in
        # webapp_config.json — the Startup-folder wrapper bat's presence
        # *is* the state, so a stale cached bool can never drift from it.
        "boot_autostart_enabled": boot_autostart.is_enabled(),
    }


@router.post("/api/config")
async def patch_config(request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    allowed = {
        "projects_dir",
        "projects_ignore",
        "coding_hidden_agents",
        "apps_scan_root",
        "team_os_dir",
        "terminal_history_lines",
        # copilot_models is deliberately absent: read-only from the UI,
        # edited in webapp_config.json directly (tenant-gated ids).
        "copilot_skip_permissions",
        "copilot_model",
        "copilot_autopilot",
        "copilot_context",
        "copilot_effort",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    # projects_ignore is a list of patterns — coerce to a clean string
    # list so a stray scalar from the client can't corrupt the config.
    if "projects_ignore" in patch:
        raw = patch["projects_ignore"]
        patch["projects_ignore"] = [
            str(p).strip() for p in (raw or []) if str(p).strip()
        ]
    # Same coercion for the hidden-button list (issue #666) — a stray scalar
    # must never reach the config as anything but a clean string list.
    if "coding_hidden_agents" in patch:
        raw = patch["coding_hidden_agents"]
        patch["coding_hidden_agents"] = [
            str(p).strip() for p in (raw or []) if str(p).strip()
        ]
    try:
        new_cfg = update_webapp_config(**patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.webapp_config = new_cfg
    return {
        "ok": True,
        "copilot_flags": build_copilot_flags(new_cfg),
    }


@router.post("/api/settings/boot-autostart")
async def set_boot_autostart(request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    enabled = bool(body.get("enabled"))
    try:
        if enabled:
            boot_autostart.enable()
        else:
            boot_autostart.disable()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "boot_autostart_enabled": boot_autostart.is_enabled()}


@router.get("/api/status")
async def status(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    tunnel_file = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"
    tunnel_url: Optional[str] = None
    if tunnel_file.exists():
        try:
            tunnel_url = tunnel_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            tunnel_url = None
    return {
        "projects_dir": cfg.projects_dir,
        "apps_scan_root": cfg.apps_scan_root,
        "tunnel_url": tunnel_url,
        "tls": cert_present(),
        "terminal": terminal_reachability(request),
    }
