"""Assembly logic for the Board tab's kanban columns (issue #300 / #164).

Four single-purpose columns (Phase 5, was five before the "Other" column —
open MRs + failed/stuck jobs — was dropped): Backlog and Done hold issues
only; Bot's turn and Your turn hold session cards only. See
:func:`build_board` for the exact routing.

Two inputs, one board:

* the **live session list** from the session-host (``session_client.list_sessions``),
* the **sessions-state file** written by the ``session_state`` hook
  (``~/.copilot/hooks/state/sessions-state.json`` — ``working`` / ``needs-you``
  / ``idle`` rows per recent coding session),

plus the in-memory GitLab snapshot from :mod:`src.gitlab_client`.

This module is now a thin ``build_board`` assembly (issue #408 split — was a
690-line god file). The three other concerns each own their own module, and
their public names are re-exported here so every existing ``board.<name>``
call site (routers, tests) keeps working unchanged:

* :mod:`src.board_state` — hook-state-file IO (:func:`read_sessions_state`,
  :func:`read_active_issues`).
* :mod:`src.board_sessions` — session-claim/merge logic
  (:func:`merge_sessions`, :func:`attach_shared_names`,
  :func:`state_row_for_session`). Writer-provided launcher id + agent wins;
  agent-less rows use an agent-gated normalized-cwd fallback.
* :mod:`src.board_transcript` — transcript-JSONL parsing
  (:func:`last_exchange`, plus the activity-overlay/external-liveness helpers
  ``merge_sessions`` calls internally).

Degradation contract (#164 acceptance): a missing/corrupt/stale state file
must never error — session cards fall back to ``unknown`` status and the
GitLab columns render regardless.

Shared session title (#396): the state row also carries ``name``/``name_source``
(the agent's live session title, where one exists). ``merge_sessions`` copies
those onto every card as ``shared_name``/``shared_name_source``, and
:func:`attach_shared_names` runs the identical agent-aware claim walk for the
Coding tab's own ``/api/coding/sessions`` list — so a live session
resolves to the same state row, and therefore the same title, on both tabs.
The frontend's precedence lives in one place, ``sessions.js``'s
``sessionTitle()``, which both tabs call.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.board_state import (  # noqa: F401 — re-exported for board.<name> callers
    STATE_STALE_AFTER,
    _age_seconds,
    _now,
    _parse_iso,
    read_active_issues,
    read_sessions_state,
)
from src.board_sessions import (  # noqa: F401 — re-exported for board.<name> callers
    active_issue_repos,
    attach_shared_names,
    merge_sessions,
    state_row_for_session,
)
from src.board_transcript import (  # noqa: F401 — re-exported
    last_exchange,
)

logger = logging.getLogger(__name__)


# #608: the family of statuses that still mean "a human needs to look at
# this" after board_transcript's needs-you split. ``idle-finished`` is
# deliberately excluded — a clean-stopped session that nothing is pending on
# isn't an alert, it's Claude quietly holding a workspace, same as ``idle``.
_NEEDS_YOU_STATUSES = frozenset({"stalled", "awaiting-decision", "awaiting-input"})


def build_board(
    session_cards: List[Dict[str, Any]],
    gitlab: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Route the two sources into the four computed columns.

    Each column holds one kind of card (#399, reduced to four in Phase 5):
    Backlog = open issues. Bot's turn = sessions working / unknown / idle /
    idle-finished (idle and idle-finished are dimmed client-side, not hidden
    — a session with nothing pending is still the bot holding a workspace,
    not an alert). Your turn = :data:`_NEEDS_YOU_STATUSES` only (#608:
    ``stalled`` / ``awaiting-decision`` / ``awaiting-input`` — the needs-you
    split's three genuinely human-actionable outcomes) — a terminal that
    needs a human. Done = today's closed issues only — a merged MR that
    closed one is already reflected by the issue itself.
    """
    bot_turn = [
        c for c in session_cards
        if c["status"] not in _NEEDS_YOU_STATUSES
    ]
    your_turn = [
        c for c in session_cards
        if c["status"] in _NEEDS_YOU_STATUSES
    ]
    return {
        "backlog": list(gitlab.get("issues") or []),
        "bot_turn": bot_turn,
        "your_turn": your_turn,
        "done": list(gitlab.get("done") or []),
    }
