"""Switching a live session's model from the Chat view's model chip.

Copilot's own in-session command does the switch, typed into the PTY like any
other prompt, and applies to that session only (CLI 1.0.114). ``/model <id>``
prints ``Model changed from GitHub Copilot • GPT-6 Luna (medium) to GitHub
Copilot • MAI Code 2 Flash (medium) for this session`` and writes a
``session.model_change`` record to the session's ``events.jsonl``, which
confirms it (:mod:`src.session_view.copilot_session`); the saved default
(``/config model``) is left as it was. The models offered are the install's own
``copilot_models``, since which ids an account may use is its plan's call.

Every other agent has no in-session switch this module can drive.
"""

from __future__ import annotations

import re
from typing import Any, List, Sequence, Tuple

Keystroke = Tuple[str, bool]

# A model id typed after `/model`: the list is free text, so an id with a space
# or a control character would type more than one command.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class SwitchRefused(Exception):
    """The switch cannot be driven here; the message is worded for the reader."""


def switch_keys(agent: Any, value: Any, offered: Sequence[str]) -> List[Keystroke]:
    """The keystrokes that switch ``agent``'s session to ``value``.

    ``offered`` is the ``copilot_models`` list. :class:`SwitchRefused` for an
    agent other than Copilot; :class:`ValueError` for a model the list does
    not offer, so nothing outside it is ever typed into a terminal.
    """
    key = str(agent or "").lower()
    if key != "copilot":
        raise SwitchRefused(f"{key.capitalize() or 'This agent'} can't switch model inside a session")
    if not isinstance(value, str) or value not in offered or not _MODEL_ID_RE.match(value):
        raise ValueError(f"model must be one this agent offers: {', '.join(offered)}")
    return [(f"/model {value}", True)]
