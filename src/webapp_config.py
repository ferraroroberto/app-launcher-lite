"""Webapp-specific configuration loader.

Lives separately from `app_config.py` because these settings are
authored from the web UI ("Save defaults" button) and persist across
runs. The CLI also reads this file so both surfaces share one source
of truth.

Holds:
- network knobs (host, port)
- scan roots for Coding projects, Apps and the Team OS skills
- the Copilot CLI launch settings and the GitLab group the Board reads
- the chief's settings
- terminal display and passkey / WebAuthn config
- Pushover failure-notification credentials
- auth secrets (bearer token + login password)

Config only — turning those settings into each agent's actual CLI argv is
launch logic, and lives in :mod:`src.launching.flags` (``build_*_flags``).
That module imports this one; never the reverse.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import MISSING, asdict, dataclass, field, replace
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

from src._json_io import atomic_write_json
from src.agents import DEFAULT_AGENT
from src.app_config import resolve_session_host_port, resolve_webapp_port

# isort: off
# Grid layout: one name per line would push this module past the structure ratchet's file-size cap.
from src.launching.presets import load_launch_presets
from src import request_timing, runtime_files
from src.chief import copilot_state
from src.chief import timings as chief_timings
from src.webapp_settings import performance
from src.webapp_settings.values import (  # noqa: F401 — re-exported for webapp_config.<name> callers
    CREDENTIAL_KEYS,
    DEFAULT_CHIEF_AUTO_COMPACT_THRESHOLD, DEFAULT_CHIEF_FALLBACK_MINUTES, DEFAULT_CHIEF_MODEL, DEFAULT_CHIEF_WORKER_CAP,
    DEFAULT_CHIEF_WORKER_CAP_SCOPE, DEFAULT_COPILOT_CONTEXT, DEFAULT_COPILOT_EFFORT,
    DEFAULT_COPILOT_MODEL, DEFAULT_COPILOT_MODELS, DEFAULT_COPILOT_MONTHLY_CREDITS, DEFAULT_HOST, DEFAULT_LARGE_UPLOAD_MAX_MB,
    DEFAULT_MIRROR_BROWSER, DEFAULT_PORT, DEFAULT_SESSION_HOST_PORT, DEFAULT_TERMINAL_HISTORY_LINES,
    MAX_CHIEF_AUTO_COMPACT_THRESHOLD, MAX_CHIEF_FALLBACK_MINUTES, MAX_CHIEF_WORKER_CAP, MAX_COPILOT_MONTHLY_CREDITS,
    MAX_LARGE_UPLOAD_MAX_MB,
    MAX_TERMINAL_HISTORY_LINES, MIN_CHIEF_AUTO_COMPACT_THRESHOLD, MIN_CHIEF_WORKER_CAP,
    MIN_COPILOT_MONTHLY_CREDITS, MIN_LARGE_UPLOAD_MAX_MB, MIN_TERMINAL_HISTORY_LINES, SESSION_HOST_PORT_ENV,
    DEFAULT_SPEECH_KEEP_RECORDINGS_HOURS, MAX_SPEECH_KEEP_RECORDINGS_HOURS, MIN_SPEECH_KEEP_RECORDINGS_HOURS,
    DEFAULT_SERVE_HTTP_PORT, DEFAULT_SERVE_OVER, VALID_SERVE_OVER,
    VALID_CHIEF_WORKER_CAP_SCOPES, VALID_COPILOT_CONTEXTS, VALID_COPILOT_EFFORTS,
    WEBAPP_CONFIG_PATH_ENV, check_mirror_browser,
)
# isort: on

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.json"



def _default_projects_dir() -> str:
    """Default to the parent of this repo (so siblings are visible)."""
    return str(PROJECT_ROOT.parent)


def _default_fleet_config_dir() -> str:
    """Default to this checkout's own ``copilot/`` tree (the chief, hooks and lane skills)."""
    return str(PROJECT_ROOT / "copilot")


def _default_sessions_state_file() -> str:
    """Where ``copilot/hooks/session_state.py`` writes the board rows."""
    return str(copilot_state.state_dir() / "sessions-state.json")


def _default_chief_plan_file() -> str:
    """Where ``copilot/skills/_lib/chief_ops.py plan`` writes the chief's plan."""
    return str(copilot_state.state_dir() / "chief-plan.json")


def _default_chief_managed_file() -> str:
    """Where ``copilot/skills/_lib/chief_managed.py`` records chief-dispatched sessions."""
    return str(copilot_state.state_dir() / "chief-managed.json")


def _default_chief_inbox_dir() -> str:
    """The chief's mailbox directory, written by ``copilot/hooks/chief_inbox.py``."""
    return str(copilot_state.state_dir() / "chief-inbox")


def _default_team_os_dir() -> str:
    """Default to the sibling ``team-os`` checkout next to this repo."""
    return str(PROJECT_ROOT.parent / "team-os")


@dataclass
class WebappConfig:
    """User-authored, persisted webapp settings."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Master folder whose direct child directories the Claude Code tab
    # lists as launchable projects.
    projects_dir: str = field(default_factory=_default_projects_dir)
    # gitignore-style patterns: directory names under `projects_dir` to
    # exclude from the Claude Code tab (matched case-insensitively, `*`
    # globs honoured). VCS / build dirs are always skipped regardless.
    projects_ignore: list = field(default_factory=list)
    # Project ids (scanner slugs) the user starred as favorites in the
    # Coding tab. Favorites sort to the top of the project
    # list and can be filtered to on their own. Stored exactly like
    # `projects_ignore` — a plain string list in this same config — so the
    # feature needs no new file.
    coding_favorites: list = field(default_factory=list)
    # Agent ids whose Coding-row launch button the user hid,
    # plus the pseudo-id `github` for the repo-issues button. A *hidden*
    # list rather than a visible one so a newly registered agent shows up
    # by default and needs no config migration. Stored like
    # `coding_favorites` — a plain string list in this same config.
    coding_hidden_agents: list = field(default_factory=list)
    # Named model + effort launch shortcuts, {id, name, agent, model, effort} rows (`src.launching.presets`).
    launch_presets: list = field(default_factory=list)
    # The one agent whose launch button stays on a Coding project row.
    # Every other visible agent — and the GitHub-issues
    # button — moved into the row's ⋯ menu, so the row is three controls
    # wide on the phone instead of nine. Stored as a plain agent id;
    # `src.agents.DEFAULT_AGENT` is the default and also the fallback the
    # config router coerces an unknown id to, so a removed agent can never
    # leave a row with no launch button at all.
    coding_favorite_agent: str = DEFAULT_AGENT
    # Where the Apps tab scans recursively for launcher `.bat` files.
    apps_scan_root: str = field(default_factory=_default_projects_dir)
    # Folder of helper `.bat` files the Apps tab lists as its Mini apps group;
    # empty (the default) scans none and hides the group.
    mini_apps_root: str = ""
    # Root of the team-os checkout the Team OS tab surfaces: skills at
    # `.claude/skills` (Copilot reads that folder too). Without the skills
    # dir the tab shows disabled, as Coding does without `projects_dir`.
    team_os_dir: str = field(default_factory=_default_team_os_dir)
    # Root of the Copilot hooks/chief tree: this repo's own ``copilot/`` unless set. It is
    # the chief's working folder, and the Board and owner checks load their helpers from it.
    fleet_config_dir: str = field(default_factory=_default_fleet_config_dir)
    # Folder holding the four issue skills (issue-add/start/yolo/finish) a lane runs,
    # for a machine whose skills live in a repo of its own. Empty uses the skills
    # bundled in the Copilot folder (`src.chief.skills_preflight`).
    skills_dir: str = ""
    # --- Board tab -----------------------------------
    # The sessions-state file written by fleet-config's session_state hook. The board
    # reads it defensively: absent/corrupt/stale degrades to unknown, never an error.
    sessions_state_file: str = field(default_factory=_default_sessions_state_file)
    # The fleet chief's plan: lanes, queue, what waits on Roberto.
    # Read defensively like sessions_state_file: absent/corrupt/unsupported
    # leaves the Board's "Chief's plan" card blank, never an error.
    chief_plan_file: str = field(default_factory=_default_chief_plan_file)
    # The session-host's reaper publishes a chief ``session-end`` event for a
    # chief-managed worker that died without running a hook: the
    # registry fleet-config's ``chief_managed.py`` writes, and the mailbox
    # directory the event goes into. Read by the session-host process only.
    chief_managed_file: str = field(default_factory=_default_chief_managed_file)
    chief_inbox_dir: str = field(default_factory=_default_chief_inbox_dir)
    # GitLab group (or user namespace) whose projects the Board's glab reads
    # span: backlog, merge requests and done-today. Empty is valid; the
    # Board then says to set it.
    gitlab_group: str = ""
    # Self-hosted GitLab instance for those reads (glab's GITLAB_HOST). Empty
    # means glab's own default context.
    gitlab_host: str = ""
    prevent_lock: bool = False  # Settings > Prevent lock (src/desktop/prevent_lock.py): pointer nudge + keep-awake, off
    # Speech to text (Settings): audio from every record button goes to
    # ``<speech_base_url>/audio/transcriptions`` in the OpenAI shape, so any
    # such server works, local or remote. ``speech_model`` and
    # ``speech_api_key`` are optional and sent only when set; the key is a
    # secret, kept in this gitignored file and never returned by the API.
    speech_enabled: bool = False
    speech_base_url: str = ""
    speech_model: str = ""
    speech_api_key: str = ""
    # Hours each dictation take stays on disk (``src/recordings.py``) so a
    # crashed page or a failed transcription loses nothing; 0 keeps none.
    speech_keep_recordings_hours: int = DEFAULT_SPEECH_KEEP_RECORDINGS_HOURS
    # How the webapp is served, read at each start (app/webapp/serve_modes.py):
    # a phone's browser only opens the microphone on an HTTPS page.
    serve_over: str = DEFAULT_SERVE_OVER
    serve_http_port: int = DEFAULT_SERVE_HTTP_PORT
    # GitHub Copilot CLI launch settings (`src.launching.flags.build_copilot_flags`).
    # `copilot_models` is the per-install list of model ids the pickers offer
    # (explicit ids are gated by the account's plan); `copilot_model` is the
    # saved `--model` ("" or "default" lets Copilot pick); `copilot_autopilot`
    # maps to `--autopilot`, `copilot_context` to `--context` ("" omits it),
    # `copilot_effort` to `--effort` (sent only with an explicit model), and
    # `copilot_skip_permissions` to `--allow-all`. `copilot_web_url_prefix`
    # only overrides the Copy link base derived from the CLI's signed-in
    # enterprise host (`src.session_view.copilot_host`).
    copilot_skip_permissions: bool = False
    copilot_web_url_prefix: str = ""
    copilot_models: list = field(default_factory=lambda: list(DEFAULT_COPILOT_MODELS))
    copilot_model: str = DEFAULT_COPILOT_MODEL
    copilot_autopilot: bool = True
    # Remote access for every Copilot session Lite launches
    # (`src.launching.copilot_remote`): on by default, off starts sessions unshared.
    copilot_remote_session: bool = True
    copilot_context: str = DEFAULT_COPILOT_CONTEXT
    copilot_effort: str = DEFAULT_COPILOT_EFFORT
    # The Usage card's fallback allowance in AI credits, named as such when the
    # plan's own allowance cannot be read (`src.copilot_usage`).
    copilot_monthly_credits: int = DEFAULT_COPILOT_MONTHLY_CREDITS
    # --- Fleet chief ----------------------------------------
    # Settings for the standing conversational orchestrator (Board chat
    # mode). Editable from the Board's chief-settings dialog; the worker
    # cap is also read by the /chief skill over loopback.
    chief_model: str = DEFAULT_CHIEF_MODEL
    # The chief's `--context` / `--effort`, from the same value sets as
    # `copilot_context` / `copilot_effort`. "" follows that Coding-agent
    # setting, so an install that never touches these launches the chief as it
    # always did; a value here overrides it for the chief alone.
    chief_context: str = ""
    chief_effort: str = ""
    chief_worker_cap: int = DEFAULT_CHIEF_WORKER_CAP
    chief_worker_cap_scope: str = DEFAULT_CHIEF_WORKER_CAP_SCOPE
    # Auto-compact threshold in percent, 0 = off. Set in Settings'
    # Chief card; served to the chief by GET /api/board/chief-settings.
    chief_auto_compact_threshold: int = DEFAULT_CHIEF_AUTO_COMPACT_THRESHOLD
    # The chief doorbell: type one fixed line into an idle chief whose
    # mailbox holds pending events. Off by default: it types into a live
    # session, and the chief only understands the line once fleet-config
    # ships its side, which is what turns this on.
    chief_doorbell: bool = False
    # The launcher's fallback wake, in minutes (0 = off): rings the idle chief
    # once when a mailbox event has waited that long or free worker slots sit
    # beside queued plan rows (app.webapp.chief_doorbell). Independent of the
    # doorbell switch, since it is the backup for a doorbell that is off or
    # missed.
    chief_fallback_minutes: int = DEFAULT_CHIEF_FALLBACK_MINUTES
    # The chief's tunable timings; defaults and ranges in src/chief/timings.py.
    # The doorbell reads the first three every tick; chief_ops.py inbox reads
    # chief_reoffer_minutes and the turn-end hook chief_hold_minutes straight
    # from this file.
    chief_rering_seconds: int = chief_timings.default("chief_rering_seconds")
    chief_unread_alert_minutes: int = chief_timings.default("chief_unread_alert_minutes")
    chief_reoffer_minutes: int = chief_timings.default("chief_reoffer_minutes")
    chief_hold_minutes: int = chief_timings.default("chief_hold_minutes")
    # Queue drain for unattended audit issues. On by default: the chief
    # adds them to its plan and dispatches into a free worker slot.
    # Settings > Chief turns it off; readers take auto_drain from the
    # chief settings payload.
    chief_auto_drain: bool = True
    # One chief-mailbox event per failed Job run / newly red main-branch CI
    # run; on by default, written only while a chief is live.
    chief_job_failures: bool = True
    chief_ci_failures: bool = True
    # Two switches fleet-config's chief reads over loopback from
    # GET /api/board/chief/settings; the app starts nothing for either. Off
    # by default: each lets the chief act on its own, so the owner opts in.
    chief_job_self_heal: bool = False
    chief_weekly_proposals: bool = False
    # Bearer token enforced when the request did NOT come from a
    # loopback IP. Empty string disables enforcement entirely.
    auth_token: str = ""
    # Optional password gate that hands the bearer token back to the
    # browser when the user types it correctly. Lets a fresh device
    # bootstrap without copy-pasting a tokenised URL.
    auth_password: str = ""
    # --- interactive phone terminal ---------------------------
    # Loopback port the PTY session-host binds (never network-reachable).
    session_host_port: int = DEFAULT_SESSION_HOST_PORT
    # Extra IPs / CIDRs allowed to reach the terminal endpoints on top of
    # loopback + the Tailscale CGNAT range (100.64.0.0/10). Empty by default.
    tailnet_allowlist: list = field(default_factory=list)
    # Trust the whole private network (10/8, 172.16/12, 192.168/16, link-local,
    # IPv6 unique-local) for the terminal routes, for a machine with no
    # Tailscale whose phone joins over a trusted home or office Wi-Fi. Off by
    # default: the bearer token and passkey still apply on top, but anything
    # else on that network can now reach the login.
    terminal_allow_private_lan: bool = False
    # The screen view shows the whole desktop, so it has its own switch on top
    # of the one above: a private address reaches it only with both on. Off by
    # default; the gate ignores it while terminal_allow_private_lan is off.
    screen_allow_private_lan: bool = False
    # Folders, besides a session's own, that an agent may send a file to the
    # Chat pane from: its outbox and Claude's SendUserFile. %VAR% and ~
    # expand, so a temp-dir root needs no machine path. Defaults to Claude's
    # scratch base, where its SendUserFile files live; a configured non-empty
    # list replaces it (an empty one reads as unset, like every list setting).
    # Settings > Folders > Chat file folders edits it, checking each
    # folder on save; this file's load stays lenient about a folder that has
    # since gone.
    outbox_scratch_roots: list = field(default_factory=lambda: ["%TEMP%/claude"])
    # Settings > Documents: the allowlist of files the reading sheet may show,
    # each {id, title, repo, path} with `repo` a folder under `projects_dir`.
    # src.documents checks a row's fields when it is used and drops a bad one,
    # so a hand edit never stops the config from loading.
    documents: list = field(default_factory=list)
    # When true, launching a session from the phone also opens an
    # interactive terminal window for it on the PC (over loopback, so it
    # bypasses the Tailscale + passkey gate). Input works from both sides.
    claude_show_local_window: bool = True
    # Whether sessions get the dedicated PC window. Off (the default), a row tap and every launch
    # (any surface, the chief and API callers included) render in the page and open no window;
    # on, a full-control session's terminal gets it. The kebab's "Open in new window" always does.
    open_sessions_in_window: bool = False
    # A session with no remembered view opens in Chat wherever Chat exists,
    # on the phone too; off restores Terminal on the phone. An explicit
    # Terminal choice and the per-session remembered view still win.
    open_sessions_in_chat: bool = True
    # Where a link tapped in Chat or a transcript opens when the page runs on
    # the PC itself: "chrome" (found through the registry's App
    # Paths, falling back to the default when absent) or "system" (the
    # Windows default browser, which is how the links opened before).
    desktop_browser: str = "chrome"
    # The browser a session's PC window opens in: "chrome", "edge" or "system" (the default browser).
    mirror_browser: str = DEFAULT_MIRROR_BROWSER
    # Bounded scrollback: how many lines of a
    # full-screen (ratatui) agent's history the session-host retains and
    # replays on a (re)connect. See DEFAULT_TERMINAL_HISTORY_LINES for the
    # real-session evidence behind the default.
    terminal_history_lines: int = DEFAULT_TERMINAL_HISTORY_LINES
    # Logs per-keystroke terminal timings and event-loop lag over 100 ms to
    # webapp/slow-requests.log. Off by default: a diagnostics switch, read live.
    terminal_timing_log: bool = False
    # Per-request-class time budgets, {class: [soft_s, hard_s]}, laid over the
    # defaults in src/request_timing.py. A request past soft is logged "over
    # budget", past hard a "budget breach". Absent classes keep their default;
    # read live.
    request_budgets: Dict[str, List[float]] = field(default_factory=dict)
    # Settings > Performance: every speed limit, its default and its hard range
    # declared once in src/webapp_settings/performance.py. All are read live, so
    # a save applies without a restart. Poll spacings are in seconds; the
    # pools are thread counts (pool_sends 0 = pick from the CPU count); the
    # transcript page cache is in pages, the tail cache in MB and the miss wait
    # in seconds.
    poll_board_s: int = performance.default("poll_board_s")
    poll_sessions_s: int = performance.default("poll_sessions_s")
    poll_transcript_s: int = performance.default("poll_transcript_s")
    poll_pause_hidden: bool = performance.default("poll_pause_hidden")
    pool_hot_poll: int = performance.default("pool_hot_poll")
    pool_session_reads: int = performance.default("pool_session_reads")
    pool_input: int = performance.default("pool_input")
    pool_sends: int = performance.default("pool_sends")
    pool_git_status: int = performance.default("pool_git_status")
    git_status_ttl_s: int = performance.default("git_status_ttl_s")
    transcript_page_cache_size: int = performance.default("transcript_page_cache_size")
    transcript_tail_cache_mb: int = performance.default("transcript_tail_cache_mb")
    transcript_resolve_miss_ttl_s: float = performance.default("transcript_resolve_miss_ttl_s")
    request_budget_scale: float = performance.default("request_budget_scale")
    # Large-file attach limit in MB, Settings-tab configurable.
    # See DEFAULT_LARGE_UPLOAD_MAX_MB.
    large_upload_max_mb: int = DEFAULT_LARGE_UPLOAD_MAX_MB
    # WebAuthn relying-party identity for the passkey gate. rp_id is the
    # bare tailnet hostname (e.g. "pc.tailnet.ts.net"); origin is the full
    # https origin the phone connects to. Empty disables the passkey gate.
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "Launcher"
    webauthn_origin: str = ""
    # --- failure summaries ---------------------------------
    # Base URL of the optional local LLM hub on this computer. It turns a failed
    # job's output tail into a one-line summary, and its
    # /admin/api/machines/health feeds the CPU sheet's machine health. Empty (the
    # default) skips both: this app has no hub dependency.
    llm_hub_url: str = ""
    # --- Jobs-tab failure notifications ---------------------
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
    # Pipe the run's output tail through the local LLM hub
    # (http://127.0.0.1:8000, claude-haiku-4-5) for a one-line
    # "what went wrong" summary prepended to the push body. Default off
    # so the issue lands without a hard dependency on the hub.
    notify_failure_summary: bool = False
    # --- Login-alert Telegram credentials -------------------
    # The vendored src/notify Telegram primitive the login lock-out alert
    # sends through. Either empty means no alert is sent.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # --- Jobs missed-fire coverage --------------------------
    # How often the webapp re-scans every scheduled job for coverage: a
    # missing/disabled Task Scheduler entry, or an elapsed slot with no run
    # record. 0 disables the background tick entirely (the `/api/jobs` badge
    # still computes lazily on poll). Deliberately *on* by default — unlike
    # the notify_* switches above it pushes nothing on its own: alerts still
    # route through notify_on_failure, which is opt-in.
    jobs_coverage_interval_minutes: int = 60
    # --- webapp/sessions retention --------------------------
    # THE retention window for the per-session audit trail: every
    # webapp/sessions/<id>.log and <id>.transcript whose mtime is older than
    # this many days is deleted by the webapp's daily sweep
    # (src/session_retention.py) — except files of a live session, which are
    # never removed. 0 keeps everything forever (the earlier behaviour).
    # Deliberately not patchable from the Settings API: a deleting setting is
    # edited here, by hand, or not at all.
    session_retention_days: int = 365
    # --- Job secrets ----------------------------------
    # One gitignored place for secret values, referenced from jobs.json by
    # opaque "$secret:<key>" strings resolved at fire time
    # (src.jobs.secrets): a job's webhook.secret and every
    # Job.env value both draw from this dict, so a rotated
    # credential lives here and never in jobs.json. Loaded from the
    # "secrets" key, falling back to the legacy "webhook_secrets" key this
    # block shipped under before it was generalized. Empty by default.
    secrets: Dict[str, str] = field(default_factory=dict)
    # --- Scoped bearer tokens, from Settings > API tokens ---
    # Salted-hash records only; each raw token is shown once and discarded.
    # api_tokens (src.auth.api_tokens): {id, label, salt, hash, scope, ...}, scope
    # "*" (like auth_token) or {"jobs": [ids]}, which fires only those jobs.
    api_tokens: list = field(default_factory=list)


def _apply_session_host_override(cfg: WebappConfig) -> WebappConfig:
    """Apply the ``LAUNCHER_SESSION_HOST_PORT`` env override, if set and valid.

    The webapp subprocess reads its session-host port from config; the e2e
    pre-ship gate sets this env var so its disposable webapp connects to a
    disposable, free-port session-host rather than adopting the live :8466.
    A missing/blank/invalid value leaves the configured port
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


def _apply_configured_ports(cfg: WebappConfig) -> WebappConfig:
    """Take both ports from ``config.json`` ``webapp``, the one place they are set.

    The tray, the ``.bat``/``.ps1`` launchers and the restart check read the
    same file, so a leftover ``port`` or ``session_host_port`` in
    ``webapp_config.json`` can never make two readers disagree about where a
    service lives. A linked worktree is the one case that reads its own
    ``port`` (see ``resolve_webapp_port``).
    """
    cfg.port = resolve_webapp_port()
    cfg.session_host_port = resolve_session_host_port()
    return cfg


def _finish_load(cfg: WebappConfig, apply_env_override: bool, validate: bool = False) -> WebappConfig:
    _apply_configured_ports(cfg)
    if apply_env_override:
        _apply_session_host_override(cfg)
    if validate:
        _validate(cfg)
    return cfg


def _load_secrets(raw: dict) -> Dict[str, str]:
    """``secrets``, honouring the legacy ``webhook_secrets`` key.

    The block holds every ``Job.env`` value, but it began as webhook secrets
    only, and old config files in the wild still spell it that way.
    """
    return {
        str(key): str(value)
        for key, value in (
            raw.get("secrets") or raw.get("webhook_secrets") or {}
        ).items()
    }


def _load_api_tokens(raw: dict) -> list:
    """``api_tokens`` — a list of *records*, not of strings, and a malformed
    entry is dropped rather than stringified: a half-written token record can
    only ever fail closed."""
    return [
        dict(token)
        for token in (raw.get("api_tokens") or [])
        if isinstance(token, dict)
    ]


def _load_documents(raw: dict) -> list:
    """``documents`` — a list of *records*; a non-object entry is dropped here
    and ``src.documents`` checks each record's fields when it is used."""
    return [
        dict(entry)
        for entry in (raw.get("documents") or [])
        if isinstance(entry, dict)
    ]


def _load_request_budgets(raw: dict) -> Dict[str, List[float]]:
    """``request_budgets`` — only the usable classes; a dropped one is logged, never fatal."""
    kept, dropped = request_timing.clean_overrides(raw.get("request_budgets"))
    for reason in dropped:
        logger.warning(f"⚠️  request_budgets ignored: {reason}")
    return kept


#: Fields whose on-disk shape can't be derived from the annotation alone.
#: Everything else is handled by the generic loop in
#: :func:`load_webapp_config`, so a new setting is declared once — on
#: :class:`WebappConfig` — and both directions follow it.
_CUSTOM_LOADERS: Dict[str, Callable[[dict], Any]] = {
    "secrets": _load_secrets,
    "api_tokens": _load_api_tokens,
    "documents": _load_documents,
    "launch_presets": load_launch_presets,
    "request_budgets": _load_request_budgets,
}


def _coerce(value: Any, annotation: Any, default: Any) -> Any:
    """Cast one raw JSON value to the type its dataclass field declares.

    ``from __future__ import annotations`` is active, so a field's ``type``
    arrives as the *string* ``"str"`` / ``"int"`` / ``"bool"`` / ``"list"``;
    both spellings are accepted so this keeps working if that import ever
    goes away. An unusable number falls back to the declared default rather
    than raising — a hand-edited config with a null in an int field
    shouldn't take the whole webapp down. Unknown annotations pass through
    untouched; those are the ones that belong in ``_CUSTOM_LOADERS``.
    """
    if annotation in (str, "str"):
        return str(value)
    if annotation in (bool, "bool"):
        # A hand-edited config's non-boolean value (e.g. the string
        # ``"false"``, truthy under `bool()`) or an unusable null (were it
        # ever to reach here) falls back to the declared default rather
        # than being coerced by truthiness — matching the int
        # branch's discipline just below.
        return value if isinstance(value, bool) else default
    if annotation in (int, "int"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if annotation in (float, "float"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if annotation in (list, "list"):
        return [str(item) for item in value]
    return value


def _load_field(spec, raw: dict) -> Any:
    """Read one :class:`WebappConfig` field out of the parsed JSON.

    Two default disciplines, both inherited from the per-field block this
    replaced and both derivable from the field itself:

    - A ``default_factory`` field (every path-ish setting, plus the list and
      dict ones) treats *any* falsy on-disk value as absent and re-derives
      the default — an empty ``projects_dir`` means "you never set one", not
      "scan the filesystem root".
    - A literal-default field keeps a falsy value as authored, because that
      is how features here are switched off: ``speech_enabled: false``
      hides the 🎤 button, an empty ``auth_token`` disables the bearer gate.

    Collapsing the two would silently re-enable whatever the user turned off,
    so the distinction is load-bearing rather than incidental. An explicit
    JSON ``null`` is the one falsy value treated as absent everywhere: it is
    never a meaningful "off" for any setting here (``""`` and ``0`` are), and
    the old per-field block turned it into ``"None"`` or a ``TypeError``
    depending on the field.
    """
    custom = _CUSTOM_LOADERS.get(spec.name)
    if custom is not None:
        return custom(raw)
    if spec.default_factory is not MISSING:  # type: ignore[misc]
        default = spec.default_factory()
        value = raw.get(spec.name)
        return _coerce(value, spec.type, default) if value else default
    default = spec.default
    value = raw.get(spec.name, default)
    if value is None:
        return default
    return _coerce(value, spec.type, default)


def _resolve_config_path(path: Optional[Path]) -> Path:
    """Resolve the config file path: explicit arg > ``LAUNCHER_WEBAPP_CONFIG``
    env override (e2e-gate isolation) > the repo default.

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


# Keys renamed since a live ``config/webapp_config.json`` may have been written
# (the file is gitignored, so a pull never rewrites it): old name -> new name.
# ``claude_config_dir`` held the fleet-config checkout root all along, not
# Claude's own config dir.
_LEGACY_KEYS = {"claude_config_dir": "fleet_config_dir"}
# The state paths whose stored value is dropped when it is the old hooks/state default.
_STATE_PATH_KEYS = {
    "sessions_state_file": "sessions-state.json",
    "chief_plan_file": "chief-plan.json",
    "chief_managed_file": "chief-managed.json",
    "chief_inbox_dir": "chief-inbox",
}


def _migrate_legacy_keys(raw: Dict[str, Any], source: Path) -> Dict[str, Any]:
    """``raw`` with each renamed key carried over to its new name, and each
    state path that holds the old ``hooks/state`` default dropped so the
    default (``src/chief/copilot_state.py``) applies.

    In memory only: the next save writes the new name and drops the old one, so
    the live file migrates itself the first time a setting changes. A new key
    already present wins over the old one.
    """
    migrated = dict(raw)
    for key, name in _STATE_PATH_KEYS.items():
        value = migrated.get(key)
        if isinstance(value, str) and copilot_state.is_legacy_default(value, name):
            # A Settings save pins every default; read through, so a migrated machine follows its state.
            del migrated[key]
    for old, new in _LEGACY_KEYS.items():
        if old not in migrated:
            continue
        legacy = migrated.pop(old)
        if new not in migrated:
            migrated[new] = legacy
            logger.info(
                f"ℹ️  {source.name}: key '{old}' is now '{new}'; read it under "
                f"the new name (the file is rewritten on the next save)"
            )
    return migrated


class ConfigUnreadableError(ValueError):
    """A save was refused because the file on disk cannot be read as settings."""


def _read_json(target: Path) -> Any:
    # utf-8-sig: a Windows editor may save the file with a byte-order mark.
    return json.loads(target.read_text(encoding="utf-8-sig"))


def _refuse_unreadable(target: Path) -> None:
    """Raise rather than let a save write over a file that holds no readable settings.

    The loader answers such a file with defaults, so the config being saved
    would replace the owner's values with them. A missing file is fine.
    """
    if not target.exists():
        return
    try:
        problem = "" if isinstance(_read_json(target), dict) else "it is not a JSON object"
    except (OSError, ValueError) as exc:
        problem = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else str(exc)
    if problem:
        logger.warning(f"⚠️  Refusing to save over {target}: {problem}")
        raise ConfigUnreadableError(
            f"{target.name} could not be read ({problem}), so nothing was saved. "
            "Fix the file or move it away, then save again."
        )


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
        return _finish_load(cfg, apply_env_override)

    try:
        raw = _read_json(target)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            f"⚠️  Could not read {target} ({exc}); falling back to defaults"
        )
        cfg = WebappConfig()
        return _finish_load(cfg, apply_env_override)

    if not isinstance(raw, dict):
        logger.warning(
            f"⚠️  {target} is not a JSON object; falling back to defaults"
        )
        cfg = WebappConfig()
        return _finish_load(cfg, apply_env_override)

    raw = _migrate_legacy_keys(raw, target)

    # One generic pass over the dataclass's own field list: the dataclass is
    # the single declaration of name, type and default both directions read.
    cfg = WebappConfig(
        **{
            spec.name: _load_field(spec, raw)
            for spec in dataclass_fields(WebappConfig)
        }
    )
    # A hand-edited speed limit out of range is put back to its default (and
    # logged) rather than refusing to start; a Settings save is refused instead.
    performance.repair(cfg)
    return _finish_load(cfg, apply_env_override, validate=True)


def save_webapp_config(cfg: WebappConfig, path: Optional[Path] = None) -> Path:
    """Atomically write the config back to disk.

    Raises :class:`ConfigUnreadableError`, writing nothing, when the file there
    now cannot be read.
    """
    target = _resolve_config_path(path)
    _refuse_unreadable(target)
    runtime_files.make_dirs(target.parent)

    # Derived straight from the dataclass — the field list, in
    # declaration order, IS the on-disk schema. asdict() also deep-copies the
    # list/dict fields, so the writer can never alias live config state.
    payload = asdict(cfg)
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


#: Enum-valued settings validated by plain membership — a new
#: enum field is one row here instead of a hand-written ``not in`` check, the
#: same declarative-table shape ``_CUSTOM_LOADERS`` above and
#: ``middleware._TERMINAL_GUARD_RULES`` already use.
_ENUM_VALIDATIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("copilot_context", VALID_COPILOT_CONTEXTS),
    ("copilot_effort", VALID_COPILOT_EFFORTS),
    ("chief_context", VALID_COPILOT_CONTEXTS),
    ("chief_effort", VALID_COPILOT_EFFORTS),
    ("chief_worker_cap_scope", VALID_CHIEF_WORKER_CAP_SCOPES),
    ("serve_over", VALID_SERVE_OVER),
)


def _validate(cfg: WebappConfig) -> None:
    if not (1 <= cfg.port <= 65535):
        raise ValueError(f"port out of range: {cfg.port}")
    if not (1 <= cfg.session_host_port <= 65535):
        raise ValueError(f"session_host_port out of range: {cfg.session_host_port}")
    if cfg.session_host_port == cfg.port:
        raise ValueError("session_host_port must differ from the webapp port")
    check_mirror_browser(cfg.mirror_browser)
    if not (1 <= cfg.serve_http_port <= 65535):
        raise ValueError(f"serve_http_port out of range: {cfg.serve_http_port}")
    # Only "both" binds it, so a clash matters only there.
    if cfg.serve_over == "both" and cfg.serve_http_port in (cfg.port, cfg.session_host_port):
        raise ValueError("serve_http_port must differ from the webapp and session-host ports")
    if not (MIN_TERMINAL_HISTORY_LINES <= cfg.terminal_history_lines <= MAX_TERMINAL_HISTORY_LINES):
        raise ValueError(
            f"terminal_history_lines must be between {MIN_TERMINAL_HISTORY_LINES} "
            f"and {MAX_TERMINAL_HISTORY_LINES}; got {cfg.terminal_history_lines}"
        )
    if not (MIN_LARGE_UPLOAD_MAX_MB <= cfg.large_upload_max_mb <= MAX_LARGE_UPLOAD_MAX_MB):
        raise ValueError(
            f"large_upload_max_mb must be between {MIN_LARGE_UPLOAD_MAX_MB} "
            f"and {MAX_LARGE_UPLOAD_MAX_MB}; got {cfg.large_upload_max_mb}"
        )
    if not (MIN_COPILOT_MONTHLY_CREDITS <= cfg.copilot_monthly_credits <= MAX_COPILOT_MONTHLY_CREDITS):
        raise ValueError(
            f"copilot_monthly_credits must be between {MIN_COPILOT_MONTHLY_CREDITS} "
            f"and {MAX_COPILOT_MONTHLY_CREDITS}; got {cfg.copilot_monthly_credits}"
        )
    if not (MIN_SPEECH_KEEP_RECORDINGS_HOURS <= cfg.speech_keep_recordings_hours
            <= MAX_SPEECH_KEEP_RECORDINGS_HOURS):
        raise ValueError(
            f"speech_keep_recordings_hours must be between {MIN_SPEECH_KEEP_RECORDINGS_HOURS} "
            f"and {MAX_SPEECH_KEEP_RECORDINGS_HOURS}; got {cfg.speech_keep_recordings_hours}"
        )
    if cfg.speech_base_url:
        parsed = urlparse(cfg.speech_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                "speech_base_url must be an http(s) URL with a host, "
                # port-host-exempt: an example in an error message, the documented default of a service this repo does not own
                f"e.g. http://127.0.0.1:8000/v1; got {cfg.speech_base_url!r}"
            )
    for problem in performance.problems(performance.values_of(cfg)):
        raise ValueError(problem)
    for field_name, allowed in _ENUM_VALIDATIONS:
        value = getattr(cfg, field_name)
        if value not in allowed:
            raise ValueError(f"{field_name} must be one of {allowed}; got {value!r}")
    if cfg.notify_failure_streak < 0:
        raise ValueError(
            f"notify_failure_streak must be >= 0; got {cfg.notify_failure_streak}"
        )
    if cfg.jobs_coverage_interval_minutes < 0:
        raise ValueError(
            "jobs_coverage_interval_minutes must be >= 0; got "
            f"{cfg.jobs_coverage_interval_minutes}"
        )
    if cfg.session_retention_days < 0:
        raise ValueError(
            f"session_retention_days must be >= 0; got {cfg.session_retention_days}"
        )
    if not (MIN_CHIEF_WORKER_CAP <= cfg.chief_worker_cap <= MAX_CHIEF_WORKER_CAP):
        raise ValueError(
            f"chief_worker_cap must be between {MIN_CHIEF_WORKER_CAP} and "
            f"{MAX_CHIEF_WORKER_CAP}; got {cfg.chief_worker_cap}"
        )
    if not cfg.copilot_models or not all(str(m).strip() for m in cfg.copilot_models):
        raise ValueError("copilot_models must be a non-empty list of model ids")
    # A saved model the edited list no longer offers falls back to Copilot's
    # own pick instead of failing the whole file; the chief's model is checked
    # at launch, where a missing id is a clear 400.
    if cfg.copilot_model not in ("", "default", *cfg.copilot_models):
        logger.warning(
            "⚠️ copilot_model %r is not in copilot_models; using Copilot's own pick",
            cfg.copilot_model,
        )
        cfg.copilot_model = ""
    _validate_chief_timers(cfg)


def _validate_chief_timers(cfg: WebappConfig) -> None:
    """The chief's timers: the fallback wake, auto-compact and the tunable timings."""
    for key in chief_timings.TUNABLE_KEYS:
        problem = chief_timings.range_problem(key, getattr(cfg, key))
        if problem:
            raise ValueError(problem)
    if not (0 <= cfg.chief_fallback_minutes <= MAX_CHIEF_FALLBACK_MINUTES):
        raise ValueError(
            "chief_fallback_minutes must be 0 (off) or up to "
            f"{MAX_CHIEF_FALLBACK_MINUTES} minutes; got {cfg.chief_fallback_minutes}"
        )
    threshold = cfg.chief_auto_compact_threshold
    if threshold != 0 and not (
        MIN_CHIEF_AUTO_COMPACT_THRESHOLD <= threshold <= MAX_CHIEF_AUTO_COMPACT_THRESHOLD
    ):
        raise ValueError(
            "chief_auto_compact_threshold must be 0 (off) or between "
            f"{MIN_CHIEF_AUTO_COMPACT_THRESHOLD} and "
            f"{MAX_CHIEF_AUTO_COMPACT_THRESHOLD} percent; got {threshold}"
        )
