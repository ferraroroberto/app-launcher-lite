"""The needs-you split for a Copilot CLI card.

The Claude Code split (:func:`src.board.transcript._refine_waiting_status`)
reads tool calls out of Claude's transcript shape. A Copilot ``events.jsonl``
has none of them, so that split found nothing and turned every Copilot turn
end into ``awaiting-input``: a lane still running a background shell, a lane
whose turn failed and a finished session all read "needs you". This split
reads Copilot's own event log instead
(:mod:`src.transcript_flavors.copilot_turn`).

A transcript that cannot be read is ``unknown``: a turn end with no evidence
is not a claim that a person is needed.
"""

from __future__ import annotations

from typing import Any

from src.board import card_status as cs
from src.transcript_flavors.copilot_turn import ASK_TOOL, read_turn_end


def copilot_waiting_status(transcript_path: Any) -> str:
    """The card value for a Copilot ``needs-you`` stamp."""
    turn = read_turn_end(transcript_path)
    if turn is None:
        return cs.UNKNOWN
    if turn.failed:
        return cs.TURN_FAILED
    if ASK_TOOL in turn.pending_tools:
        return cs.AWAITING_DECISION  # a question on screen waits on a person, whatever else runs
    if turn.background_pending:
        return cs.WAITING_ON_TASK
    if turn.pending_tools:
        return cs.TOOL_PENDING
    return cs.IDLE_FINISHED
