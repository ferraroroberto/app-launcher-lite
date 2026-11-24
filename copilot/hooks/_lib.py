"""Shared helpers for app-launcher-lite's Copilot CLI hooks.

Every hook here follows the same contract as the parent fleet-config repo:

* Reads a single JSON payload from stdin.
* Never blocks anything — these hooks are advisory-only state writers.
* Always exits 0, even on failure, so a broken hook can never disturb the
  Copilot session it observes.

GitHub Copilot CLI's hook payloads are **camelCase** and carry **no event
name field** (verified live against Copilot CLI 1.0.70, 2026-08-01):

* ``userPromptSubmitted`` → ``{"sessionId", "timestamp", "cwd", "prompt"}``
* ``sessionStart``        → ``{..., "source", "initialPrompt"}``
* ``agentStop``           → ``{..., "transcriptPath", "stopReason", "stop_hook_active"}``
* ``sessionEnd``          → ``{..., "reason"}``

``timestamp`` is epoch milliseconds. Because the payload does not name its
own event, the hook definition JSON passes the event name as ``argv[1]`` to the
hook script — see ``hooks/session-state.template.json``.

``normalize_payload`` converts the camelCase envelope to the snake_case
vocabulary the writers read (``session_id``, ``cwd``, ``transcript_path``),
so a future harness that already speaks snake_case passes through unchanged.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")

# Outside hooks/: Copilot CLI loads every .json below it as a hook config. The webapp's copy of
# this rule is src/chief/copilot_state.py; tests/unit/copilot/test_state_location.py pins them equal.
STATE_DIRNAME = "app-launcher-lite-state"


def state_dir() -> Path:
    """The state folder: the override, else the old hooks/state until install.py --migrate moves it, else the new one."""
    root = os.environ.get("COPILOT_HOOKS_STATE_DIR")
    if root:
        return Path(root)
    home = Path.home() / ".copilot"
    current, legacy = home / STATE_DIRNAME, home / "hooks" / "state"
    return legacy if legacy.is_dir() and not current.is_dir() else current


# camelCase → snake_case for the envelope fields the writers actually read.
# Anything else falls through the generic converter so new fields still
# arrive under a predictable name.
_KEYS = {
    "sessionId": "session_id",
    "transcriptPath": "transcript_path",
    "initialPrompt": "initial_prompt",
    "stopReason": "stop_reason",
}


def _camel_to_snake(key: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", key).lower()


def normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a camelCase Copilot payload into snake_case.

    A payload already carrying ``session_id`` is returned unchanged (same
    object), so this is a strict pass-through for snake_case producers.
    """
    if not isinstance(payload, dict) or "session_id" in payload:
        return payload
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        out[_KEYS.get(key) or _camel_to_snake(key)] = value
    return out


def read_stdin_json() -> Dict[str, Any]:
    """Read the hook payload from stdin; {} on empty/unparseable input.

    The hook command invokes Python directly with no pipe
    (``& python session_state.py <event>``): a native command started without
    pipeline input inherits PowerShell's raw stdin, so Copilot's UTF-8 bytes
    arrive untouched in Windows PowerShell 5.1 and pwsh 7 alike. The old
    ``[Console]::In.ReadToEnd() | python`` pipe would decode them with the console
    OEM code page first, garbling any non-ASCII text.
    The bytes are decoded as UTF-8 here, independent of the locale default.
    """
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return normalize_payload(data)


def cwd(payload: Dict[str, Any]) -> Path:
    """Best-effort working directory for the event."""
    raw = payload.get("cwd")
    if isinstance(raw, str) and raw:
        return Path(raw)
    return Path.cwd()


def allow() -> None:
    """Exit 0 silently — the only way a lite hook ever ends."""
    sys.exit(0)


# The decision-log writer mirrors the other tier's copy on purpose: the tree
# boundary forbids either tier importing the other.
# jscpd:ignore-start
DECISIONS_LOG_NAME = "chief-decisions.log"
# Rolled to `.1` past this size, so a year of lanes cannot grow it without bound;
# Save diagnostics reads both files.
DECISIONS_LOG_MAX_BYTES = 5 * 1024 * 1024


def note_decision(log_dir: Path, source: str, message: str) -> None:
    """Append `<local time> [INFO] <source>: <message>` to `<log_dir>/chief-decisions.log`.

    The trail Settings > Save diagnostics reads for mailbox writes, claims,
    acks and lane marks, which run in hook and helper processes with no log of
    their own. Advisory: a failed write is dropped, never raised.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    target = log_dir / DECISIONS_LOG_NAME
    try:
        # runtime-write-exempt: the decisions log sits beside the chief mailbox in the user state dir
        log_dir.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > DECISIONS_LOG_MAX_BYTES:
            # must-use-exempt: rolls the log aside by renaming it; nothing is written
            os.replace(target, target.with_name(target.name + ".1"))
        # runtime-write-exempt: the decisions log sits beside the chief mailbox in the user state dir
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [INFO] {source}: {message}\n")
    except OSError:
        pass
# jscpd:ignore-end
