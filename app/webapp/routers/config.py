"""Launcher config read/patch + machine status readout.

`/api/status` exposes the tunnel URL, TLS cert presence, and a
terminal-reachability hint so the SPA can explain up front when the
live terminal won't work on the current connection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.webapp.routers._helpers import (
    PROJECT_ROOT,
    cert_present,
    maybe_json,
)
from src import boot_autostart, password, private_network
from src.agents import AGENTS, DEFAULT_AGENT
from src.chief.launch_settings import chief_model_problem
from src.desktop import lid_keep_awake
from src.launching import copilot_remote
from src.launching.flags import build_copilot_flags, missing_default_copilot_models
from src.launching.presets import (
    PRESET_CONTEXTS,
    PresetError,
    checked_launch_presets,
    efforts_for,
)
from src.middleware import private_network_switch_gate, terminal_reachability
from src.session_view import outbox as session_outbox
from src.webapp_config import (
    MAX_CHIEF_AUTO_COMPACT_THRESHOLD,
    MAX_CHIEF_WORKER_CAP,
    MAX_LARGE_UPLOAD_MAX_MB,
    MAX_TERMINAL_HISTORY_LINES,
    MIN_CHIEF_AUTO_COMPACT_THRESHOLD,
    MIN_CHIEF_WORKER_CAP,
    MIN_LARGE_UPLOAD_MAX_MB,
    MIN_TERMINAL_HISTORY_LINES,
    VALID_COPILOT_CONTEXTS,
    VALID_COPILOT_EFFORTS,
    WebappConfig,
    update_webapp_config,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/config")
async def get_config(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return {
        "host": cfg.host,
        "port": cfg.port,
        "projects_dir": cfg.projects_dir,
        "projects_ignore": cfg.projects_ignore,
        # Folders, besides a session's own, an agent may send Chat files from:
        # the text as typed, so Settings shows what the owner wrote.
        "outbox_scratch_roots": cfg.outbox_scratch_roots,
        # Coding-row buttons the user hid; the SPA filters the row strip on this.
        "coding_hidden_agents": cfg.coding_hidden_agents,
        "coding_favorite_agent": cfg.coding_favorite_agent,
        # The named model + effort presets the dropdowns offer.
        "launch_presets": cfg.launch_presets,
        # Where the SPA opens a session: the PC window, and Chat first.
        "open_sessions_in_window": cfg.open_sessions_in_window,
        "open_sessions_in_chat": cfg.open_sessions_in_chat,
        # Settings › Terminal: trust the private network for the terminal-grade
        # routes, and (a second switch) for the screen view too.
        "terminal_allow_private_lan": cfg.terminal_allow_private_lan,
        "screen_allow_private_lan": cfg.screen_allow_private_lan,
        "apps_scan_root": cfg.apps_scan_root,
        "team_os_dir": cfg.team_os_dir,
        "fleet_config_dir": cfg.fleet_config_dir,
        # The forge the Board reads: a GitLab group and, for a self-hosted
        # instance, its host.
        "gitlab_group": cfg.gitlab_group,
        "gitlab_host": cfg.gitlab_host,
        # Speech to text. The key is write-only: only whether one is set goes out.
        "speech": {
            "enabled": cfg.speech_enabled,
            "base_url": cfg.speech_base_url,
            "model": cfg.speech_model,
            "api_key_set": bool(cfg.speech_api_key),
        },
        "terminal_history_lines": cfg.terminal_history_lines,
        "terminal_history_lines_min": MIN_TERMINAL_HISTORY_LINES,
        "terminal_history_lines_max": MAX_TERMINAL_HISTORY_LINES,
        # The compose bar's large-file attach limit, in MB.
        "large_upload_max_mb": cfg.large_upload_max_mb,
        "large_upload_max_mb_min": MIN_LARGE_UPLOAD_MAX_MB,
        "large_upload_max_mb_max": MAX_LARGE_UPLOAD_MAX_MB,
        # The chief's auto-compact threshold: percent, 0 = off.
        "chief_auto_compact_threshold": cfg.chief_auto_compact_threshold,
        "chief_auto_compact_threshold_min": MIN_CHIEF_AUTO_COMPACT_THRESHOLD,
        "chief_auto_compact_threshold_max": MAX_CHIEF_AUTO_COMPACT_THRESHOLD,
        "chief_doorbell": cfg.chief_doorbell,
        "chief_auto_drain": cfg.chief_auto_drain,
        "chief_job_failures": cfg.chief_job_failures,
        "chief_job_self_heal": cfg.chief_job_self_heal,
        "chief_weekly_proposals": cfg.chief_weekly_proposals,
        # The chief's model, context, effort and worker cap, read-only here so
        # the Settings > Chief row can show them without a passkey prompt.
        # Writes stay on the passkey-gated PUT /api/board/chief/settings.
        "chief_model": cfg.chief_model,
        "chief_model_problem": chief_model_problem(cfg),
        "chief_context": cfg.chief_context,
        "chief_effort": cfg.chief_effort,
        "chief_worker_cap": cfg.chief_worker_cap,
        "chief_worker_cap_scope": cfg.chief_worker_cap_scope,
        "chief_worker_cap_min": MIN_CHIEF_WORKER_CAP,
        "chief_worker_cap_max": MAX_CHIEF_WORKER_CAP,
        "copilot": copilot_payload(cfg),
        "auth_password_set": password.is_configured(legacy=cfg.auth_password),
        # Boot-autostart toggle: live-queried, not stored in
        # webapp_config.json — the Startup-folder wrapper bat's presence
        # *is* the state, so a stale cached bool can never drift from it.
        "boot_autostart_enabled": boot_autostart.is_enabled(),
        # powercfg is a subprocess, so it is read off the event loop; a PC
        # with no lid reports "unsupported".
        "lid_keep_awake": await asyncio.to_thread(lid_keep_awake.payload),
    }


def copilot_payload(cfg: WebappConfig) -> Dict[str, Any]:
    """The Copilot launch settings, the choices Settings offers and the flags they build."""
    return {
        "skip_permissions": cfg.copilot_skip_permissions,
        "models_available": list(cfg.copilot_models),
        "models_missing_defaults": missing_default_copilot_models(cfg),
        "model": cfg.copilot_model,
        "autopilot": cfg.copilot_autopilot,
        "remote_session": cfg.copilot_remote_session,
        "remote_cli_state": copilot_remote.read_state(),
        "context": cfg.copilot_context,
        "contexts_available": list(VALID_COPILOT_CONTEXTS),
        # What a launch preset may pick: each listed model's efforts (the catalog
        # narrows one, any other model takes every level) and the context tiers.
        "preset_efforts": {
            model: list(efforts_for("copilot", model)) for model in cfg.copilot_models
        },
        "preset_contexts": list(PRESET_CONTEXTS),
        "effort": cfg.copilot_effort,
        "efforts_available": list(VALID_COPILOT_EFFORTS),
        "computed_flags": build_copilot_flags(cfg),
    }


def _checked_outbox_roots(raw: Any, current: List[str]) -> List[str]:
    """The trimmed send-folder list, or a 400 naming the first entry that
    cannot be one. A stored entry (in ``current``) is forgiven a folder that
    has since gone, so that cannot block removing a different one; only a new
    entry must exist."""
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise HTTPException(status_code=400, detail="outbox_scratch_roots must be a list of folders")
    entries = [p.strip() for p in raw if p.strip()]
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="Keep at least one folder: an empty list would bring the default back.",
        )
    for index, entry in enumerate(entries):
        try:
            session_outbox.check_root(
                entry, entries[:index], forgive_missing=entry in current
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return entries


def _checked_model_list(raw: Any) -> List[str]:
    """The Copilot model list: trimmed, de-duplicated, at least one id."""
    if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
        raise HTTPException(status_code=400, detail="copilot_models must be a list of model ids")
    models = list(dict.fromkeys(m.strip() for m in raw if m.strip()))
    if not models:
        raise HTTPException(status_code=400, detail="copilot_models needs at least one model id")
    return models


# The two switches that widen who reaches the terminal-grade routes; changing
# either is gated on the caller (private_network_switch_gate).
PRIVATE_NETWORK_KEYS = ("terminal_allow_private_lan", "screen_allow_private_lan")


# The Settings switches: a truthy string must never flip
# one ("false" is truthy in Python), so patch_config refuses anything but a bool.
_SWITCH_KEYS = (
    "open_sessions_in_window",
    "open_sessions_in_chat",
    "chief_doorbell",
    "chief_auto_drain",
    "chief_job_failures",
    "chief_job_self_heal",
    "chief_weekly_proposals",
    "copilot_skip_permissions",
    "copilot_autopilot",
    "copilot_remote_session",
    "speech_enabled",
    *PRIVATE_NETWORK_KEYS,
)


# The settings POST /api/config may change; any other body key is ignored.
_PATCH_KEYS = frozenset({
    "projects_dir",
    "projects_ignore",
    "outbox_scratch_roots",
    "coding_hidden_agents",
    "coding_favorite_agent",
    "launch_presets",
    "open_sessions_in_window",
    "open_sessions_in_chat",
    *PRIVATE_NETWORK_KEYS,
    "apps_scan_root",
    "team_os_dir",
    "fleet_config_dir",
    "gitlab_group",
    "gitlab_host",
    "speech_enabled",
    "speech_base_url",
    "speech_model",
    "speech_api_key",
    "terminal_history_lines",
    "large_upload_max_mb",
    "chief_auto_compact_threshold",
    "chief_doorbell",
    "chief_auto_drain",
    "chief_job_failures",
    "chief_job_self_heal",
    "chief_weekly_proposals",
    "copilot_skip_permissions",
    "copilot_models",
    "copilot_model",
    "copilot_autopilot",
    "copilot_remote_session",
    "copilot_context",
    "copilot_effort",
})


def _checked_whole_number(patch: Dict[str, Any], key: str, what: str) -> None:
    """Refuse a bool or a float for a whole-number setting (a bool is an int in
    Python and a float would be silently truncated); the range is _validate's."""
    if key in patch:
        value = patch[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=400, detail=f"{key} must be {what}")


def _settle_private_network(patch: Dict[str, Any], cfg: WebappConfig) -> None:
    """Keep the screen switch under the terminal one: turning the terminal
    switch off turns the screen off with it, and the screen switch cannot be
    turned on while the terminal one is off (a 400)."""
    terminal_on = patch.get("terminal_allow_private_lan", cfg.terminal_allow_private_lan)
    if not terminal_on:
        if patch.get("screen_allow_private_lan"):
            raise HTTPException(
                status_code=400,
                detail="screen_allow_private_lan needs terminal_allow_private_lan on first",
            )
        patch["screen_allow_private_lan"] = False
    logger.info(
        "ℹ️  private-network switches: terminal=%s screen=%s",
        terminal_on, patch.get("screen_allow_private_lan", cfg.screen_allow_private_lan),
    )


@router.post("/api/config", response_model=None)
async def patch_config(request: Request) -> Any:
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    patch = {k: v for k, v in body.items() if k in _PATCH_KEYS}
    # projects_ignore is a list of patterns — coerce to a clean string
    # list so a stray scalar from the client can't corrupt the config.
    if "projects_ignore" in patch:
        raw = patch["projects_ignore"]
        patch["projects_ignore"] = [
            str(p).strip() for p in (raw or []) if str(p).strip()
        ]
    # The Chat send folders are checked on write, here, and not in
    # `_validate`: that also runs on load, where a folder deleted since it was
    # saved must not take the whole file down.
    if "outbox_scratch_roots" in patch:
        patch["outbox_scratch_roots"] = _checked_outbox_roots(
            patch["outbox_scratch_roots"], cfg.outbox_scratch_roots
        )
    if "coding_hidden_agents" in patch:
        patch["coding_hidden_agents"] = [
            str(p).strip() for p in (patch["coding_hidden_agents"] or []) if str(p).strip()
        ]
    if "launch_presets" in patch:
        try:
            patch["launch_presets"] = checked_launch_presets(patch["launch_presets"])
        except PresetError as exc:
            # The field and row ride along so Settings can show the message
            # beside the control it is about.
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "field": exc.field, "index": exc.index},
            )
    if "copilot_models" in patch:
        patch["copilot_models"] = _checked_model_list(patch["copilot_models"])
    for key in (
        "gitlab_group", "gitlab_host", "copilot_model", "copilot_context", "copilot_effort",
        "speech_base_url", "speech_model", "speech_api_key",
    ):
        if key in patch:
            patch[key] = str(patch[key] or "").strip()
    _checked_whole_number(patch, "chief_auto_compact_threshold", "an integer percent")
    _checked_whole_number(patch, "large_upload_max_mb", "a whole number of MB")
    for key in _SWITCH_KEYS:
        if key in patch and not isinstance(patch[key], bool):
            raise HTTPException(
                status_code=400, detail=f"{key} must be true or false"
            )
    if any(key in patch for key in PRIVATE_NETWORK_KEYS):
        refusal = private_network_switch_gate(request)
        if refusal is not None:
            return refusal
        _settle_private_network(patch, cfg)
    # The favourite agent must name a registered agent; an unknown id is
    # coerced to DEFAULT_AGENT rather than stored, because the row renders
    # exactly one launch button and a dangling id would leave it with none.
    if "coding_favorite_agent" in patch:
        wanted = str(patch["coding_favorite_agent"] or "").strip()
        patch["coding_favorite_agent"] = (
            wanted if wanted in AGENTS else DEFAULT_AGENT
        )
    try:
        new_cfg = update_webapp_config(**patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.webapp_config = new_cfg
    return {"ok": True, "copilot": copilot_payload(new_cfg)}


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


@router.post("/api/settings/lid-keep-awake")
async def set_lid_keep_awake(request: Request) -> Dict[str, Any]:
    """Set the active power plan's lid close action to Do nothing, or restore
    the values saved before. No elevation is needed, so a failure is a 400
    carrying the plain reason."""
    body = await maybe_json(request)
    change = lid_keep_awake.enable if bool(body.get("enabled")) else lid_keep_awake.disable
    try:
        await asyncio.to_thread(change)
    except lid_keep_awake.LidKeepAwakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "lid_keep_awake": await asyncio.to_thread(lid_keep_awake.payload)}


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
        # The PC load sheet's IP row; null until discovery has found one.
        "private_ip": private_network.current(),
    }


@router.post("/api/status/refresh")
async def status_refresh() -> Dict[str, Any]:
    """Re-read this PC's private IP when the PC load sheet opens.

    ``discovery_ok`` false means nothing was found this time; the address is
    then the last one cached (null when there never was one).
    """
    discovery_ok = await asyncio.to_thread(private_network.refresh)
    return {"private_ip": private_network.current(), "discovery_ok": discovery_ok}
