"""Terminal-agent registries for Coding launches and loopback PTY clients.

The Coding tab launches an interactive terminal agent in a project
folder, hosted by the session-host PTY/remote machinery:

- ``copilot`` — GitHub Copilot CLI (GitHub's terminal-native agentic
  coding agent; authenticates in-session via ``/login``).

This module is the single source of truth for the agent id → command
mapping. ``AGENTS`` contains the coding agents exposed by the webapp;
``SESSION_HOST_AGENTS`` adds loopback-only PTY commands such as SSH. The
split keeps service integrations out of the Coding tab while letting the
session-host reuse its normal ConPTY/WebSocket machinery for them.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Dict, List

from src.env_path import effective_path


@dataclass(frozen=True)
class Agent:
    """One command that the terminal session-host can launch.

    ``id`` is the stable key threaded through the launch API; ``label``
    is the display name; ``command`` is the executable resolved off
    ``PATH`` when the agent is spawned; ``quit_command`` is the
    interactive command typed into the PTY for a graceful stop (each
    program uses its own — Copilot's and SSH's are ``/exit`` and
    ``exit`` respectively).

    ``fullscreen`` marks a full-screen *differential* TUI that repaints
    in place rather than scrolling inline. The session-host streams these
    differently: it must **not** replay the raw scrollback ring on
    (re)connect — doing so dumps stale move-cursor/clear deltas into a
    fresh xterm and re-answers the agent's startup terminal queries (the
    ``[?1;2c`` DA leak, issue #128) — and instead forces a clean repaint.

    ``resume_token`` is the agent's *native* resume invocation, spliced
    between the command and the flags for a Resume launch (issue #151) so
    the agent renders its own session picker over the PTY — the launcher
    never builds a session list of its own (Copilot's is ``--resume``).
    Empty means the agent has no resume path.

    ``native_name_flag`` is a spawn-time flag that gives a new session a
    picker-visible title. It is intentionally separate from a live rename:
    the launcher must never type into a running agent TUI (#555). Empty means
    the agent exposes no verified non-PTY naming mechanism.
    """

    id: str
    label: str
    command: str
    quit_command: str
    fullscreen: bool = False
    resume_token: str = ""
    native_name_flag: str = ""


# id → Agent. The order here is the order the Coding tab renders the
# per-tile launch buttons in.
AGENTS: Dict[str, Agent] = {
    "copilot": Agent(
        id="copilot", label="GitHub Copilot CLI", command="copilot",
        quit_command="/exit", fullscreen=True, resume_token="--resume",
        native_name_flag="--name",
    ),
}

# The loopback session-host accepts the Coding-tab agents plus commands used
# by trusted sibling services. SSH is intentionally absent from ``AGENTS``:
# it is a service integration, not a Coding-tab launch button (#558).
SESSION_HOST_AGENTS: Dict[str, Agent] = {
    **AGENTS,
    "ssh": Agent(
        id="ssh", label="SSH", command="ssh",
        quit_command="exit", fullscreen=False,
    ),
}

DEFAULT_AGENT = "copilot"


def command_for(agent_id: str) -> str:
    """Return the PATH command for ``agent_id``.

    Raises :class:`ValueError` for an unknown id so a bad value can
    never silently fall through to spawning the wrong process.
    """
    agent = SESSION_HOST_AGENTS.get(agent_id)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id!r}")
    return agent.command


def quit_command_for(agent_id: str) -> str:
    """Return the interactive quit command for ``agent_id``.

    Typed into the PTY for a graceful "Stop" (the terminal window stays
    open while the agent exits cleanly). Falls back to the default
    agent's command for an unknown id rather than raising — a bad id
    must never block a stop.
    """
    agent = SESSION_HOST_AGENTS.get(agent_id) or AGENTS[DEFAULT_AGENT]
    return agent.quit_command


def resume_command_for(agent_id: str) -> str:
    """Return the agent's native resume token (issue #151).

    Spliced between the command and the flags for a Resume launch so the
    agent shows its own session picker (Copilot ``--resume``). Returns an
    empty string for an unknown id or an agent with no resume path; the
    caller treats that as "not resumable" rather than raising, so a bad id
    can never break a launch.
    """
    agent = SESSION_HOST_AGENTS.get(agent_id)
    return agent.resume_token if agent else ""


def native_session_name_flags_for(agent_id: str, title: str) -> str:
    """Return safe spawn-time picker-name flags for ``title`` (issue #556).

    The title originates outside the command line (for example, from a
    GitHub issue). Only pass it through ``cmd.exe`` when it contains no
    metacharacters; otherwise the launcher-side ``manual_title`` remains the
    safe source of truth. Unsupported agents also return an empty string.
    """
    agent = SESSION_HOST_AGENTS.get(agent_id)
    clean = title.strip()[:60]
    if not agent or not agent.native_name_flag or not clean:
        return ""
    if any(not (char.isalnum() or char in " .,:;()[]{}_-/#?") for char in clean):
        return ""
    return f'{agent.native_name_flag} "{clean}"'


def is_fullscreen(agent_id: str) -> bool:
    """Whether ``agent_id`` is a full-screen differential TUI.

    Drives the session-host's (re)connect handling: full-screen agents
    skip the raw scrollback-ring replay and get a forced repaint instead
    (issue #128). An unknown id is treated as non-fullscreen — the safe
    inline default.
    """
    agent = SESSION_HOST_AGENTS.get(agent_id)
    return bool(agent and agent.fullscreen)


def is_installed(agent_id: str) -> bool:
    """Whether ``agent_id``'s command resolves on the **effective** PATH.

    Not the inherited one (issue #668): a CLI installed while the launcher
    is running writes its directory into the registry, which no running
    process ever re-reads — so ``shutil.which`` against ``os.environ`` would
    keep reporting "not installed" until Explorer restarted or the user
    logged off. :func:`src.env_path.effective_path` folds the registry
    values in, and the session-host spawns children with the same merged
    path, so detection and launch can't disagree.
    """
    agent = SESSION_HOST_AGENTS.get(agent_id)
    if agent is None:
        return False
    return shutil.which(agent.command, path=effective_path()) is not None


def detect_agents() -> List[Dict[str, object]]:
    """Detection snapshot for the SPA — one dict per known agent.

    Each dict is ``{"id", "label", "available", "fullscreen"}``;
    ``available`` is the live ``PATH`` check, and ``fullscreen`` lets the
    SPA tell a differential TUI apart from an inline-scrolling agent so
    the phone terminal can pan the fixed canvas above the keyboard instead
    of reflowing — reflowing resizes the PTY and makes a differential TUI
    repaint on every keyboard open/close (issue #264). The Coding tab
    disables an agent's launch button (with a hover hint) when
    ``available`` is ``False``.
    """
    return [
        {
            "id": agent.id,
            "label": agent.label,
            "available": is_installed(agent.id),
            "fullscreen": agent.fullscreen,
        }
        for agent in AGENTS.values()
    ]
