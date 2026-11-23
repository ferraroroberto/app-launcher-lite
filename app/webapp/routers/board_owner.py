"""Owner checks: the Board's answers to work waiting on the owner.

    POST /api/board/owner-checks/answer → apply the answer sheet's owner-check
                                          answers (Tailscale + passkey)

The list itself rides ``GET /api/board`` as its ``owner_checks`` section, read
from :mod:`src.board.owner_checks`'s cache; ``POST /api/board/github/refresh``
starts that cache's background refresh. Mounted on ``board.router`` like
:mod:`app.webapp.routers.board_chief`, so ``server.py`` still registers one
Board router.

Writes reach GitLab with the owner's own ``glab`` login: Validated and Done
through the fleet-config checkout's ``owner_check clear``, a Problem as a plain
note. Pending, Pause and Close write no GitLab state: a launcher mark, and the
sheet's message tells the chief.
Each answer is applied on its own and reported on its own, so one failed write
never hides the ones that landed.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.webapp.route_pools import OUTBOUND, run_on
from app.webapp.routers._helpers import maybe_json
from src.board import owner_checks
from src.clients import gitlab as forge
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
router = APIRouter()

# Bounds one request's GitLab writes; the sheet sends a long list in several
# requests well under it (owner-answer.js OWNER_ANSWERS_PER_REQUEST).
_MAX_ANSWERS = 30


def _apply(cfg: WebappConfig, ans: Dict[str, Any]) -> Dict[str, Any]:
    def comment(repo: str, number: int, body: str) -> None:
        forge.comment_issue(cfg.gitlab_group, repo, number, body, cfg.gitlab_host)

    try:
        owner_checks.answer(cfg.fleet_config_dir, ans, comment=comment)
    except (owner_checks.OwnerCheckError, forge.GlabError, OSError) as exc:
        logger.warning("⚠️ owner check %s: %s not applied: %s", ans["ref"], ans["answer"], exc)
        return {"ref": ans["ref"], "answer": ans["answer"], "ok": False, "error": str(exc)}
    return {"ref": ans["ref"], "answer": ans["answer"], "ok": True}


@router.post("/api/board/owner-checks/answer")
async def post_owner_answers(request: Request) -> Dict[str, Any]:
    """Body ``{"answers": [{ref, kind, answer, text?}]}``; one result per answer.

    ``answer`` is ``validated`` / ``problem`` for a validate-after item and
    ``done`` / ``pending`` / ``pause`` / ``close`` for a before-start one; a
    problem carries its ``text``. A malformed answer fails the whole request (400) before any write.
    """
    body = await maybe_json(request)
    raw = body.get("answers")
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_ANSWERS:
        count = len(raw) if isinstance(raw, list) else "no"
        logger.warning("⚠️ owner-check answers refused: %s answers, the cap is %d", count, _MAX_ANSWERS)
        raise HTTPException(status_code=400, detail=f"answers must be a list of 1 to {_MAX_ANSWERS}")
    try:
        answers = [owner_checks.validate_answer(a) for a in raw]
    except ValueError as exc:
        logger.warning("⚠️ owner-check answers refused (%d answers): %s", len(raw), exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg: WebappConfig = request.app.state.webapp_config
    results: List[Dict[str, Any]] = []
    for ans in answers:
        results.append(await run_on(OUTBOUND, _apply, cfg, ans))
    failed = [r["ref"] for r in results if not r["ok"]]
    kinds = Counter(a["answer"] for a in answers)
    logger.info(
        "ℹ️ owner-check answers: %d received (%s), %d applied, %d failed%s",
        len(answers), ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())),
        len(answers) - len(failed), len(failed), f": {', '.join(failed)}" if failed else "",
    )
    return {"results": results, "owner_checks": owner_checks.snapshot()}
