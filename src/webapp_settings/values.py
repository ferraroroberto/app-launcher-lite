"""The defaults and value sets each :class:`src.webapp_config.WebappConfig` field
validates against: network ports, credential keys, display bounds and the
Copilot launch choices.

:mod:`src.webapp_config` re-exports every name here, so callers keep reading
them from there.
"""

from __future__ import annotations

from typing import Tuple

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8465
# Loopback port the PTY session-host binds. Never network-reachable.
DEFAULT_SESSION_HOST_PORT = 8466
# Env override for the session-host port. Set ONLY by the e2e pre-ship gate's
# autoboot so a disposable webapp can be pointed at a disposable, free-port
# session-host instead of the live :8466 a running tray owns. This is what
# stops the gate from reaching into — and killing — the user's real PTY
# sessions. Not a user-facing knob; intentionally undocumented
# in the config sample.
SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"
# Env override for the config file *path* itself. Set ONLY by the e2e
# pre-ship gate's autoboot (tests/e2e/fixtures/autoboot.py) so the disposable webapp
# reads AND writes its own temp config (derived from the real one, minus every
# credential) instead of the real, shared
# config/webapp_config.json — a Settings-tab e2e test that clicks Save must
# never mutate the user's real file (a port corruption
# was this exact shared-file design biting). Not a user-facing knob;
# intentionally undocumented in the config sample.
WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
# Every on-disk key that holds a credential. Anything that derives
# a config for a test or disposable run from the real file must drop these —
# the e2e autoboot does (tests/_credential_hygiene.py), and its failure-output
# redaction scrubs their values. ``webhook_secrets`` is the legacy name
# ``secrets`` still loads from (see ``_load_secrets``), so it is a credential
# too. A new credential field must be added here: tests/unit/repo/test_credential_hygiene.py
# fails on any credential-shaped field name this tuple doesn't list.
CREDENTIAL_KEYS: Tuple[str, ...] = (
    "auth_token",
    "auth_password",
    "pushover_api_token",
    "pushover_user_key",
    "telegram_bot_token",
    "secrets",
    "webhook_secrets",
    "api_tokens",
    "speech_api_key",
)

# Bounded scrollback for full-screen (ratatui) agent sessions —
# how many lines of history the session-host retains and
# replays on a (re)connect. 10,000 is evidence-based, not a guess: a real,
# tool-heavy Codex exchange was observed producing 3371-4447 total scrolled
# lines and rendering to only ~216-283 KB even at that size (see
# src/vt_snapshot.py's _HISTORY_LINES docstring for the full history). The
# bounds keep a user-set value sane: too low reintroduces "can't see the
# start of a real conversation"; too high risks a slow reconnect paint on
# a weak mobile connection.
DEFAULT_TERMINAL_HISTORY_LINES = 10_000
MIN_TERMINAL_HISTORY_LINES = 200
MAX_TERMINAL_HISTORY_LINES = 50_000

# The compose bar's large-file attach: the most it will take,
# in MB. Separate from the ordinary attach's fixed 12 MB, because a large file
# is stored for the agent to use by path and never read into its context.
# 2 GB covers an archive of scanned documents with room to spare; the ceiling
# keeps a typo from turning into an unbounded write to the project drive.
DEFAULT_LARGE_UPLOAD_MAX_MB = 2048
MIN_LARGE_UPLOAD_MAX_MB = 1
MAX_LARGE_UPLOAD_MAX_MB = 10240

# --- Fleet chief ---------------------------------------
# The standing conversational orchestrator the Board's chat mode talks to.
# `chief_model` must be a dispatchable Claude tier; the worker cap is read
# by the /chief skill over loopback as its dispatch rail — it caps how many
# worker sessions the chief may have running at once. The
# daily-respawn setting is retired: fleet-config shipped compact-and-continue
# (chief hands its own handover log back to itself on every session start),
# so an unattended daily respawn would now discard a live batch's context
# rather than protect it. A restart is still available, but only as an
# explicit operator action (the Board's chief Restart button) — never
# a schedule that fires unattended.
# The chief runs on Copilot like every lane: its model is one of the
# per-install `copilot_models` (checked at launch).
DEFAULT_CHIEF_MODEL = "gpt-6-luna"
DEFAULT_CHIEF_WORKER_CAP = 3
MIN_CHIEF_WORKER_CAP = 1
MAX_CHIEF_WORKER_CAP = 10
# What the worker cap counts: "chief" = only sessions the chief dispatched,
# "all" = every live session but the chief's and the telegram: channels.
# fleet-config's dispatch rail reads it over loopback and treats a missing
# value as "chief".
VALID_CHIEF_WORKER_CAP_SCOPES = ("chief", "all")
DEFAULT_CHIEF_WORKER_CAP_SCOPE = "chief"
# The context-use percent at which the chief compacts itself (read by
# fleet-config's `chief_ops.py self-compact` from
# GET /api/board/chief-settings). 0 turns it off; otherwise it must sit in
# the range. The app only stores the number and never compacts anything.
DEFAULT_CHIEF_AUTO_COMPACT_THRESHOLD = 30
MIN_CHIEF_AUTO_COMPACT_THRESHOLD = 10
MAX_CHIEF_AUTO_COMPACT_THRESHOLD = 90

# Copilot CLI launch values. The model ids are a starting list for a fresh
# install: which ids an account may use depends on its plan, so the list is
# edited per install in Settings. "" (or "default") omits `--model`.
DEFAULT_COPILOT_MODELS = [
    "gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "mai-code-2-flash",
]
DEFAULT_COPILOT_MODEL = "gpt-6-luna"
# `--context` values the CLI accepts; "" omits the flag.
VALID_COPILOT_CONTEXTS = ("", "default", "long_context")
DEFAULT_COPILOT_CONTEXT = "long_context"
# `--effort` values the CLI accepts; "" omits the flag. The auto model
# rejects any effort, so the flag goes out only beside an explicit model.
VALID_COPILOT_EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_COPILOT_EFFORT = "xhigh"
