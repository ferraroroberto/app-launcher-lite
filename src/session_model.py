"""The model a live session runs, as the id Copilot knows it by.

Derived on the webapp side from the spawn ``flags`` the session-host already
reports, plus the ``transcript_model`` the webapp reads from the session's
event log (:func:`src.board.sessions.attach_transcript_model`). Never from a
new session-host field: a change to the session-host's import closure is not
live until ``:8466`` restarts, and that restart kills every live session.

Never guesses. A session launched on the agent's default model carries no
``--model`` flag, so until it has logged one it gets ``None`` (no pill), not a
plausible-looking wrong name.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Mapping, Optional

from src.agents import agent_of


def _flag_value(flags: str, name: str) -> Optional[str]:
    """The value following ``name`` in a flags string, or ``None``."""
    try:
        tokens = shlex.split(flags or "", posix=False)
    except ValueError:
        tokens = (flags or "").split()
    for index, token in enumerate(tokens[:-1]):
        if token == name:
            return tokens[index + 1].strip("\"'")
    return None


def session_model_label(session: Mapping[str, Any]) -> Optional[str]:
    """The id of the session's model, or ``None``. Copilot logs the id it
    runs, and the Settings and Switch model lists name models by that same id."""
    if agent_of(session) != "copilot":
        return None
    answered = str(session.get("transcript_model") or "")
    return answered or _flag_value(str(session.get("flags") or ""), "--model") or None


def attach_models(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each session plus a ``model`` field (a model id or ``None``).

    Returns new dicts; the inputs are never mutated.
    """
    return [{**sess, "model": session_model_label(sess)} for sess in sessions]
