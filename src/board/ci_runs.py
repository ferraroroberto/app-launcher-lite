"""Red GitLab pipelines on each project's default branch, as Board cards.

The Board's forge refresh (``POST /api/board/github/refresh``) reads every
project's default-branch pipelines into the GitLab cache
(:func:`src.clients.gitlab.read_pipelines`); ``GET /api/board`` turns that
snapshot into cards with :func:`board_cards`, a pure memory read, so no
pipeline call ever rides the 5 s poll.

A project is ``red`` when its latest finished default-branch pipeline failed,
``green`` when it did not, and ``unknown`` when its pipelines could not be read
or its default branch could not be named: an unreadable project is never
green. A green project and one with no pipeline make no card, and a later
green pipeline drops the red card. The card is not dismissible: the job
dismissal store is keyed by job run, and a red branch is a live problem that
leaves when the branch goes green.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.board.attention_state import AttentionState
from src.board.state import age_seconds, parse_iso
from src.clients import gitlab as forge

#: Names the whole read in the one card shown when no project is known to speak for.
_GROUP_LABEL = "GitLab pipelines"


def board_cards(ci: Dict[str, Any], *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """One ``failed`` card per red project, one ``unknown`` card per unreadable one.

    ``ci`` is the ``ci`` block of the GitLab snapshot. When the project list
    could not be read and no project was ever known, a single ``unknown`` card
    stands for the whole read, so the failure is never a silent green.
    """
    moment = now or datetime.now(timezone.utc)
    projects = ci.get("projects") or {}
    cards: List[Dict[str, Any]] = []
    for key in sorted(projects):
        result = projects[key]
        repo = result.get("repo") or key
        if result.get("state") == forge.CI_UNKNOWN:
            cards.append(_card(repo, AttentionState.UNKNOWN, error=result.get("reason") or ""))
        elif result.get("state") == forge.CI_RED:
            since = parse_iso(result.get("red_since"))
            cards.append(_card(
                repo, AttentionState.FAILED, branch=result.get("branch"),
                pipeline_id=result.get("pipeline_id"), pipeline_url=result.get("pipeline_url"),
                age=age_seconds(since, moment) if since else None,
            ))
    if not projects and ci.get("error"):
        cards.append(_card(_GROUP_LABEL, AttentionState.UNKNOWN, error=str(ci["error"])))
    return cards


def _card(
    repo: str, state: AttentionState, *, branch: Any = None, pipeline_id: Any = None,
    pipeline_url: Any = None, age: Optional[int] = None, error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "kind": "ci", "repo": repo, "state": state, "branch": branch,
        "pipeline_id": pipeline_id, "pipeline_url": pipeline_url,
        "age_seconds": age, "error": error or None,
    }
