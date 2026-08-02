"""Webapp-specific configuration loader.

Lives separately from `app_config.py` because these settings are
authored from the web UI ("Save defaults" button) and persist across
runs. The CLI also reads this file so both surfaces share one source
of truth.

Holds:
- network knobs (host, port)
- scan roots for coding projects and Apps
- the Copilot launch *settings* (model list, model, autopilot, context,
  effort) together with the ``VALID_*`` value sets the fields validate
  against
- Team OS tab settings
- terminal display and passkey / WebAuthn config
- Pushover failure-notification credentials
- auth secrets (bearer token + login password)

Config only — turning those settings into the agent's actual CLI argv is
launch logic, and lives in :mod:`src.launch_flags` (``build_*_flags``, split
out in issue #691). That module imports this one; never the reverse.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlencode, urlparse, urlunparse

from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.json"
SAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.sample.json"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8465
# Loopback port the PTY session-host binds. Never network-reachable.
DEFAULT_SESSION_HOST_PORT = 8466
# Env override for the session-host port. Set ONLY by the e2e pre-ship gate's
# autoboot so a disposable webapp can be pointed at a disposable, free-port
# session-host instead of the live :8466 a running tray owns. This is what
# stops the gate from reaching into — and killing — the user's real PTY
# sessions (issue #260). Not a user-facing knob; intentionally undocumented
# in the config sample.
SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"
# Env override for the config file *path* itself. Set ONLY by the e2e
# pre-ship gate's autoboot (tests/e2e/conftest.py) so the disposable webapp
# reads AND writes a temp copy of the config instead of the real, shared
# config/webapp_config.json — a Settings-tab e2e test that clicks Save must
# never mutate the user's real file (issue #441; the #438 port corruption
# was this exact shared-file design biting). Not a user-facing knob;
# intentionally undocumented in the config sample.
WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"

# Bounded scrollback for full-screen agent sessions (issue #435 follow-up) —
# how many lines of history the session-host retains and replays on a
# (re)connect. 10,000 is evidence-based, not a guess: a real, tool-heavy
# full-screen agent exchange was observed producing 3371-4447 total scrolled
# lines and rendering to only ~216-283 KB even at that size (see
# src/vt_snapshot.py's _HISTORY_LINES docstring for the full history). The
# bounds keep a user-set value sane: too low reintroduces "can't see the
# start of a real conversation"; too high risks a slow reconnect paint on
# a weak mobile connection.
DEFAULT_TERMINAL_HISTORY_LINES = 10_000
MIN_TERMINAL_HISTORY_LINES = 200
MAX_TERMINAL_HISTORY_LINES = 50_000

# Models the GitHub Copilot CLI accepts for the `--model` flag (and the
# in-session `/model` command) — a *config list*, not a hardcoded tuple:
# explicit model ids are tenant-gated, so the offered set is edited in
# webapp_config.json per install. The empty string (or the "default"
# sentinel) means "don't pass --model" — Copilot then picks its own (auto) —
# and is always allowed in `copilot_model` regardless of the list.
DEFAULT_COPILOT_MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
DEFAULT_COPILOT_MODEL = "gpt-5.6-luna"
# `--context` values the Copilot CLI accepts (1.0.70). "" = omit the flag.
VALID_COPILOT_CONTEXTS = ("", "default", "long_context")
DEFAULT_COPILOT_CONTEXT = "long_context"
# `--effort` values the Copilot CLI accepts (1.0.70). "" = omit the flag.
# NOTE: the auto model (no --model) rejects --effort outright ("does not
# support reasoning effort configuration"), so build_copilot_flags only
# emits it alongside an explicit model.
VALID_COPILOT_EFFORTS = (
    "", "none", "minimal", "low", "medium", "high", "xhigh", "max"
)
DEFAULT_COPILOT_EFFORT = "xhigh"


def _default_projects_dir() -> str:
    """Default to the parent of this repo (so siblings are visible)."""
    return str(PROJECT_ROOT.parent)


def _default_team_os_dir() -> str:
    """Default to the sibling ``team-os`` checkout next to this repo."""
    return str(PROJECT_ROOT.parent / "team-os")


def _default_sessions_state_file() -> str:
    """Where fleet-config-lite's ``session_state`` hook writes the board rows."""
    return str(Path.home() / ".copilot" / "hooks" / "state" / "sessions-state.json")


@dataclass
class WebappConfig:
    """User-authored, persisted webapp settings."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Master folder whose direct child directories the Coding tab
    # lists as launchable projects.
    projects_dir: str = field(default_factory=_default_projects_dir)
    # gitignore-style patterns: directory names under `projects_dir` to
    # exclude from the Coding tab (matched case-insensitively, `*`
    # globs honoured). VCS / build dirs are always skipped regardless.
    projects_ignore: list = field(default_factory=list)
    # Project ids (scanner slugs) the user starred as favorites in the
    # Coding tab (issue #250). Favorites sort to the top of the project
    # list and can be filtered to on their own. Stored exactly like
    # `projects_ignore` — a plain string list in this same config — so the
    # feature needs no new file.
    coding_favorites: list = field(default_factory=list)
    # Agent ids whose Coding-row launch button the user hid (issue #666),
    # plus the pseudo-id `github` for the repo-issues button. A *hidden*
    # list rather than a visible one so a newly registered agent shows up
    # by default and needs no config migration. Stored like
    # `coding_favorites` — a plain string list in this same config.
    coding_hidden_agents: list = field(default_factory=list)
    # Where the Apps tab scans recursively for launcher `.bat` files.
    apps_scan_root: str = field(default_factory=_default_projects_dir)
    # Root of the team-os checkout the Team OS tab surfaces (issue #102).
    # Skills live at `<team_os_dir>/.claude/skills`, identity at
    # `<team_os_dir>/identity`. When the skills dir doesn't exist the tab
    # shows disabled, the same way the Coding tab handles a missing
    # `projects_dir`.
    team_os_dir: str = field(default_factory=_default_team_os_dir)
    # --- Board tab (issue #300 / #164) -----------------------------------
    # The sessions-state file written by fleet-config-lite's session_state
    # hook. The board reads it defensively — absent/corrupt/stale degrades
    # to unknown session status, never an error.
    sessions_state_file: str = field(default_factory=_default_sessions_state_file)
    # GitLab group whose projects the Board's glab queries span (backlog /
    # done-today). Subgroups are included by the group endpoint. Empty is
    # valid — the Board's GitLab panel just shows a "set gitlab_group in
    # Settings" hint instead of running any subprocess.
    gitlab_group: str = ""
    # Self-hosted GitLab instance for the queries (rides glab's GITLAB_HOST
    # env var). Empty = glab's own default context (gitlab.com or whatever
    # `glab auth login` configured).
    gitlab_host: str = ""
    # GitHub Copilot CLI launch settings (issue #48; config-driven since the
    # lite fork's Phase 3). `copilot_models` is the UI/select list of model
    # ids offered — read-only from the UI, edited in the JSON file directly
    # (explicit ids are tenant-gated). `copilot_model` is the persisted
    # `--model` value ("" or "default" = let Copilot pick auto);
    # `copilot_skip_permissions` is the opt-in allow-all switch;
    # `copilot_autopilot` maps to `--autopilot`; `copilot_context` to
    # `--context` ("" = omit); `copilot_effort` to `--effort` ("" = omit,
    # and only ever emitted alongside an explicit model — see
    # build_copilot_flags).
    copilot_skip_permissions: bool = False
    copilot_models: list = field(
        default_factory=lambda: list(DEFAULT_COPILOT_MODELS)
    )
    copilot_model: str = DEFAULT_COPILOT_MODEL
    copilot_autopilot: bool = True
    copilot_context: str = DEFAULT_COPILOT_CONTEXT
    copilot_effort: str = DEFAULT_COPILOT_EFFORT
    # Bearer token enforced when the request did NOT come from a
    # loopback IP. Empty string disables enforcement entirely.
    auth_token: str = ""
    # Optional password gate that hands the bearer token back to the
    # browser when the user types it correctly. Lets a fresh device
    # bootstrap without copy-pasting a tokenised URL.
    auth_password: str = ""
    # --- interactive phone terminal (issue #1) ---------------------------
    # Loopback port the PTY session-host binds (never network-reachable).
    session_host_port: int = DEFAULT_SESSION_HOST_PORT
    # Extra IPs / CIDRs allowed to reach the terminal endpoints on top of
    # loopback + the Tailscale CGNAT range (100.64.0.0/10). Empty by
    # default. This is also how a non-Tailscale VPN enables the terminal:
    # add its client subnet (the tailnet_ name is historical).
    tailnet_allowlist: list = field(default_factory=list)
    # When true, launching a session from the phone also opens an
    # interactive terminal window for it on the PC (over loopback, so it
    # bypasses the private-network + passkey gate). Input works from both sides.
    show_local_window: bool = True
    # Bounded scrollback (issue #435 follow-up): how many lines of a
    # full-screen agent's history the session-host retains and replays on
    # a (re)connect. See DEFAULT_TERMINAL_HISTORY_LINES for the
    # real-session evidence behind the default.
    terminal_history_lines: int = DEFAULT_TERMINAL_HISTORY_LINES
    # WebAuthn relying-party identity for the passkey gate. rp_id is the
    # bare tailnet hostname (e.g. "pc.tailnet.ts.net"); origin is the full
    # https origin the phone connects to. Empty disables the passkey gate.
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "App Launcher Lite"
    webauthn_origin: str = ""
    # --- Jobs-tab failure notifications (issue #66) ---------------------
    # Pushover credentials — both empty means no-op notifier (executor
    # still finalises runs identically). The master switch
    # `notify_on_failure` defaults off so the feature ships dormant.
    pushover_api_token: str = ""
    pushover_user_key: str = ""
    notify_on_failure: bool = False
    # Also fire when the consecutive-failure streak hits this count
    # (useful when single-failure pushes are muted via Pushover quiet
    # hours). 0 disables the streak fire.
    notify_failure_streak: int = 0
    # --- Per-job Telegram failure alerts (issue #597) --------------------
    # Credentials for the vendored src/notify Telegram primitive. Both
    # empty means no-op — the executor still finalises runs identically.
    # Unlike notify_on_failure above (global, Pushover, every job), this
    # channel fires per job: only jobs with Job.alert_on_failure=True push
    # here, so the shared Telegram chat isn't spammed by every failure.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # --- Jobs missed-fire coverage (issue #697) --------------------------
    # How often the webapp re-scans every scheduled job for coverage: a
    # missing/disabled Task Scheduler entry, or an elapsed slot with no run
    # record. 0 disables the background tick entirely (the `/api/jobs` badge
    # still computes lazily on poll). Deliberately *on* by default — unlike
    # the notify_* switches above it pushes nothing on its own: alerts still
    # route through notify_on_failure / Job.alert_on_failure, both opt-in.
    jobs_coverage_interval_minutes: int = 60
    # --- Job secrets (issues #73, #72) ----------------------------------
    # One gitignored place for secret values, referenced from jobs.json by
    # opaque "$secret:<key>" strings resolved at fire time
    # (src.jobs_secrets): a job's webhook.secret (issue #73) and every
    # Job.env value (issue #72) both draw from this dict, so a rotated
    # credential lives here and never in jobs.json. Loaded from the
    # "secrets" key, falling back to the legacy "webhook_secrets" key this
    # block shipped under before #72 generalized it. Empty by default.
    secrets: Dict[str, str] = field(default_factory=dict)
    # --- Scoped API bearer tokens (issue #72) ---------------------------
    # List of token records minted from the Settings tab (see
    # src.api_tokens): {id, label, salt, hash, scope, created_at,
    # last_used_at}. Only the salted SHA-256 hash is stored — the raw
    # token is shown once at mint time and discarded. scope is "*" or
    # {"jobs": [ids]}; a job-scoped token can only fire its allowed jobs
    # (POST /api/jobs/<id>/run) and nothing else. The legacy auth_token
    # above keeps working unchanged with implicit scope "*".
    api_tokens: list = field(default_factory=list)


def _apply_session_host_override(cfg: WebappConfig) -> WebappConfig:
    """Apply the ``LAUNCHER_SESSION_HOST_PORT`` env override, if set and valid.

    The webapp subprocess reads its session-host port from config; the e2e
    pre-ship gate sets this env var so its disposable webapp connects to a
    disposable, free-port session-host rather than adopting the live :8466
    (issue #260). A missing/blank/invalid value leaves the configured port
    untouched, so normal runs are unaffected.
    """
    raw = os.environ.get(SESSION_HOST_PORT_ENV, "").strip()
    if not raw:
        return cfg
    try:
        port = int(raw)
    except ValueError:
        logger.warning(
            "⚠️  ignoring non-integer %s=%r", SESSION_HOST_PORT_ENV, raw
        )
        return cfg
    if not (1 <= port <= 65535):
        logger.warning(
            "⚠️  ignoring out-of-range %s=%d", SESSION_HOST_PORT_ENV, port
        )
        return cfg
    cfg.session_host_port = port
    return cfg


def _resolve_config_path(path: Optional[Path]) -> Path:
    """Resolve the config file path: explicit arg > ``LAUNCHER_WEBAPP_CONFIG``
    env override (e2e-gate isolation, issue #441) > the repo default.

    Consulted by both :func:`load_webapp_config` and
    :func:`save_webapp_config` so the override is symmetric — a process
    pointed at a temp copy reads and writes that copy, never half of each.
    """
    if path is not None:
        return Path(path)
    env = os.environ.get(WEBAPP_CONFIG_PATH_ENV, "").strip()
    if env:
        return Path(env)
    return DEFAULT_CONFIG_PATH


def load_webapp_config(
    path: Optional[Path] = None, *, apply_env_override: bool = True
) -> WebappConfig:
    """Load the webapp config, falling back to defaults if the file is missing.

    ``apply_env_override=False`` skips the ``LAUNCHER_SESSION_HOST_PORT``
    override (see :func:`_apply_session_host_override`) — used by
    :func:`update_webapp_config` so a config PATCH made against the e2e
    gate's disposable autobooted webapp (which sets that env var) can never
    bake the disposable session-host port into the real, shared
    ``config/webapp_config.json`` on save. Every other caller wants the
    override applied, hence the default.
    """
    target = _resolve_config_path(path)
    if not target.exists():
        logger.info(
            f"📂 webapp_config not found at {target}, using defaults "
            f"(file will be created when settings change)"
        )
        cfg = WebappConfig()
        return _apply_session_host_override(cfg) if apply_env_override else cfg

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            f"⚠️  Could not read {target} ({exc}); falling back to defaults"
        )
        cfg = WebappConfig()
        return _apply_session_host_override(cfg) if apply_env_override else cfg

    cfg = WebappConfig(
        host=str(raw.get("host", DEFAULT_HOST)),
        port=int(raw.get("port", DEFAULT_PORT)),
        projects_dir=str(raw.get("projects_dir") or _default_projects_dir()),
        projects_ignore=[str(p) for p in (raw.get("projects_ignore") or [])],
        coding_favorites=[str(p) for p in (raw.get("coding_favorites") or [])],
        coding_hidden_agents=[
            str(p) for p in (raw.get("coding_hidden_agents") or [])
        ],
        apps_scan_root=str(raw.get("apps_scan_root") or _default_projects_dir()),
        team_os_dir=str(raw.get("team_os_dir") or _default_team_os_dir()),
        sessions_state_file=str(
            raw.get("sessions_state_file") or _default_sessions_state_file()
        ),
        gitlab_group=str(raw.get("gitlab_group", "") or ""),
        gitlab_host=str(raw.get("gitlab_host", "") or ""),
        copilot_skip_permissions=bool(
            raw.get("copilot_skip_permissions", False)
        ),
        copilot_models=(
            [str(m) for m in raw["copilot_models"]]
            if isinstance(raw.get("copilot_models"), list)
            else list(DEFAULT_COPILOT_MODELS)
        ),
        copilot_model=str(raw.get("copilot_model", DEFAULT_COPILOT_MODEL)),
        copilot_autopilot=bool(raw.get("copilot_autopilot", True)),
        copilot_context=str(
            raw.get("copilot_context", DEFAULT_COPILOT_CONTEXT)
        ),
        copilot_effort=str(raw.get("copilot_effort", DEFAULT_COPILOT_EFFORT)),
        auth_token=str(raw.get("auth_token", "")),
        auth_password=str(raw.get("auth_password", "")),
        session_host_port=int(
            raw.get("session_host_port", DEFAULT_SESSION_HOST_PORT)
        ),
        tailnet_allowlist=list(raw.get("tailnet_allowlist") or []),
        show_local_window=bool(
            raw.get("show_local_window", True)
        ),
        terminal_history_lines=int(
            raw.get("terminal_history_lines", DEFAULT_TERMINAL_HISTORY_LINES)
        ),
        webauthn_rp_id=str(raw.get("webauthn_rp_id", "")),
        webauthn_rp_name=str(raw.get("webauthn_rp_name", "App Launcher Lite")),
        webauthn_origin=str(raw.get("webauthn_origin", "")),
        pushover_api_token=str(raw.get("pushover_api_token", "")),
        pushover_user_key=str(raw.get("pushover_user_key", "")),
        notify_on_failure=bool(raw.get("notify_on_failure", False)),
        notify_failure_streak=int(raw.get("notify_failure_streak", 0) or 0),
        telegram_bot_token=str(raw.get("telegram_bot_token", "")),
        telegram_chat_id=str(raw.get("telegram_chat_id", "")),
        jobs_coverage_interval_minutes=int(
            raw.get("jobs_coverage_interval_minutes", 60) or 0
        ),
        secrets={
            str(k): str(v)
            for k, v in (
                raw.get("secrets") or raw.get("webhook_secrets") or {}
            ).items()
        },
        api_tokens=[
            dict(t) for t in (raw.get("api_tokens") or []) if isinstance(t, dict)
        ],
    )
    if apply_env_override:
        _apply_session_host_override(cfg)
    _validate(cfg)
    return cfg


def save_webapp_config(cfg: WebappConfig, path: Optional[Path] = None) -> Path:
    """Atomically write the config back to disk."""
    target = _resolve_config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "host": cfg.host,
        "port": cfg.port,
        "projects_dir": cfg.projects_dir,
        "projects_ignore": cfg.projects_ignore,
        "coding_favorites": cfg.coding_favorites,
        "coding_hidden_agents": cfg.coding_hidden_agents,
        "apps_scan_root": cfg.apps_scan_root,
        "team_os_dir": cfg.team_os_dir,
        "sessions_state_file": cfg.sessions_state_file,
        "gitlab_group": cfg.gitlab_group,
        "gitlab_host": cfg.gitlab_host,
        "copilot_skip_permissions": cfg.copilot_skip_permissions,
        "copilot_models": cfg.copilot_models,
        "copilot_model": cfg.copilot_model,
        "copilot_autopilot": cfg.copilot_autopilot,
        "copilot_context": cfg.copilot_context,
        "copilot_effort": cfg.copilot_effort,
        "auth_token": cfg.auth_token,
        "auth_password": cfg.auth_password,
        "session_host_port": cfg.session_host_port,
        "tailnet_allowlist": cfg.tailnet_allowlist,
        "show_local_window": cfg.show_local_window,
        "terminal_history_lines": cfg.terminal_history_lines,
        "webauthn_rp_id": cfg.webauthn_rp_id,
        "webauthn_rp_name": cfg.webauthn_rp_name,
        "webauthn_origin": cfg.webauthn_origin,
        "pushover_api_token": cfg.pushover_api_token,
        "pushover_user_key": cfg.pushover_user_key,
        "notify_on_failure": cfg.notify_on_failure,
        "notify_failure_streak": cfg.notify_failure_streak,
        "telegram_bot_token": cfg.telegram_bot_token,
        "telegram_chat_id": cfg.telegram_chat_id,
        "jobs_coverage_interval_minutes": cfg.jobs_coverage_interval_minutes,
        "secrets": cfg.secrets,
        "api_tokens": cfg.api_tokens,
    }

    atomic_write_json(target, payload)
    logger.info(f"💾 Saved webapp_config to {target}")
    return target


def update_webapp_config(**fields) -> WebappConfig:
    """Read, patch, save — convenience for the API endpoint.

    Loads the *un-overridden* on-disk config (``apply_env_override=False``)
    so a save can never bake the e2e gate's disposable
    ``LAUNCHER_SESSION_HOST_PORT`` into the real, shared
    ``config/webapp_config.json`` — that env var is a runtime-only override
    for the disposable autobooted webapp, never something to persist. The
    returned config still carries the override applied, so the caller's own
    in-process state (e.g. the running webapp's ``app.state.webapp_config``)
    keeps talking to the right session-host for the rest of that process's
    life — only the on-disk file is protected.
    """
    current = load_webapp_config(apply_env_override=False)
    patched = replace(current, **fields)
    _validate(patched)
    save_webapp_config(patched)
    return _apply_session_host_override(patched)


def append_auth_token(url: str, token: Optional[str]) -> str:
    """Return ``url`` with ``?token=<token>`` appended when ``token`` is set."""
    if not token:
        return url
    parsed = urlparse(url)
    existing = parsed.query
    extra = urlencode({"token": token})
    new_query = f"{existing}&{extra}" if existing else extra
    return urlunparse(parsed._replace(query=new_query))


def _validate(cfg: WebappConfig) -> None:
    # GitLab knobs are plain strings; normalize rather than raise — stray
    # whitespace (or a non-string from a hand-edited file) must never brick
    # config loading. Empty gitlab_group is valid (Board shows a hint).
    cfg.gitlab_group = str(cfg.gitlab_group or "").strip()
    cfg.gitlab_host = str(cfg.gitlab_host or "").strip()
    if not (1 <= cfg.port <= 65535):
        raise ValueError(f"port out of range: {cfg.port}")
    if not (1 <= cfg.session_host_port <= 65535):
        raise ValueError(
            f"session_host_port out of range: {cfg.session_host_port}"
        )
    if cfg.session_host_port == cfg.port:
        raise ValueError("session_host_port must differ from the webapp port")
    if not (MIN_TERMINAL_HISTORY_LINES <= cfg.terminal_history_lines <= MAX_TERMINAL_HISTORY_LINES):
        raise ValueError(
            f"terminal_history_lines must be between {MIN_TERMINAL_HISTORY_LINES} "
            f"and {MAX_TERMINAL_HISTORY_LINES}; got {cfg.terminal_history_lines}"
        )
    # Copilot knobs self-heal instead of raising: the model list is a
    # hand-edited config field (tenant-gated ids), so a stale persisted
    # value must never brick config loading — fall back with a warning.
    if not (
        isinstance(cfg.copilot_models, list)
        and all(isinstance(m, str) and m.strip() for m in cfg.copilot_models)
    ):
        logger.warning(
            "⚠️  copilot_models must be a list of non-empty strings; got %r "
            "— falling back to defaults", cfg.copilot_models
        )
        cfg.copilot_models = list(DEFAULT_COPILOT_MODELS)
    # "" / "default" always allowed: "let Copilot pick (auto)".
    if cfg.copilot_model not in ("", "default") and (
        cfg.copilot_model not in cfg.copilot_models
    ):
        logger.warning(
            "⚠️  copilot_model %r is not in copilot_models %r — falling "
            "back to '' (Copilot auto)", cfg.copilot_model, cfg.copilot_models
        )
        cfg.copilot_model = ""
    if cfg.copilot_context not in VALID_COPILOT_CONTEXTS:
        logger.warning(
            "⚠️  copilot_context must be one of %r; got %r — falling back "
            "to %r", VALID_COPILOT_CONTEXTS, cfg.copilot_context,
            DEFAULT_COPILOT_CONTEXT,
        )
        cfg.copilot_context = DEFAULT_COPILOT_CONTEXT
    if cfg.copilot_effort not in VALID_COPILOT_EFFORTS:
        logger.warning(
            "⚠️  copilot_effort must be one of %r; got %r — falling back "
            "to %r", VALID_COPILOT_EFFORTS, cfg.copilot_effort,
            DEFAULT_COPILOT_EFFORT,
        )
        cfg.copilot_effort = DEFAULT_COPILOT_EFFORT
    if cfg.notify_failure_streak < 0:
        raise ValueError(
            f"notify_failure_streak must be >= 0; got {cfg.notify_failure_streak}"
        )
    if cfg.jobs_coverage_interval_minutes < 0:
        raise ValueError(
            "jobs_coverage_interval_minutes must be >= 0; got "
            f"{cfg.jobs_coverage_interval_minutes}"
        )
