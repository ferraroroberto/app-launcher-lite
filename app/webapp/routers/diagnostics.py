"""Settings > Diagnostics: save the last N minutes of every Lite log to one file.

``POST /api/diagnostics``          ``{minutes}`` -> ``{name, path, records, bytes}``
``GET  /api/request-stats``        in-flight peak, budget verdicts per class, git-status cache age
``GET  /api/diagnostics/{name}``   -> that saved file, as plain text

The file is written on this PC by :mod:`src.diagnostics_bundle`, redacted
before it is written, and never sent anywhere; the GET is what lets the phone
download it. Both are passkey-gated (``src/middleware/routes.py``): the file
holds session lifecycle and chief traffic, terminal-grade content.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src import diagnostics_bundle, request_timing
from src.build_info import build_identity
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
router = APIRouter()

#: The build this process loaded, named in the file beside the checkout's.
_SERVED_SHA = build_identity().get("git_sha", "unknown")


class SaveRequest(BaseModel):
    minutes: int = 60


def _git_status_age_s(request: Request) -> Optional[float]:
    """Seconds since the last git-status scan began, or ``None`` when none has run.

    Reads the cache the git-status route keeps on ``app.state`` and never
    scans: asking must not start the work it reports on.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    cache = getattr(request.app.state, "git_status_cache", None) or {}
    cached = cache.get((str(cfg.projects_dir), tuple(cfg.projects_ignore)))
    return None if cached is None else round(time.monotonic() - cached[0], 1)


@router.get("/api/request-stats")
async def request_stats(
    request: Request, window_s: float = Query(default=300.0, gt=0, le=900)
) -> Dict[str, Any]:
    """Read-only request health: what the timing middleware has seen since boot.

    ``max_inflight`` is the highest in-flight count in the last ``window_s``
    seconds, the number a benchmark reports beside its latencies. ``budgets``
    is the table in force, defaults with the config's ``request_budgets``
    laid over them.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    budgets = request_timing.resolve_budgets(cfg.request_budgets)
    return {
        **request_timing.STATS.snapshot(window_s),
        "budgets": {
            name: {"soft_s": b.soft_s, "hard_s": b.hard_s} for name, b in budgets.items()
        },
        "git_status_age_s": _git_status_age_s(request),
    }


@router.post("/api/diagnostics")
async def save_diagnostics(body: SaveRequest, request: Request) -> Dict[str, Any]:
    if body.minutes not in diagnostics_bundle.WINDOW_CHOICES_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=f"minutes must be one of {list(diagnostics_bundle.WINDOW_CHOICES_MINUTES)}",
        )
    cfg: WebappConfig = request.app.state.webapp_config
    try:
        saved = await asyncio.to_thread(
            diagnostics_bundle.save_bundle,
            diagnostics_bundle.sources_from_config(cfg),
            body.minutes,
            literals=diagnostics_bundle.configured_secrets(cfg),
            served_sha=_SERVED_SHA,
        )
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ diagnostics not saved: {exc}")
        raise HTTPException(status_code=500, detail=f"Diagnostics could not be saved: {exc}") from exc
    return {
        "name": saved.path.name,
        "path": str(saved.path),
        "records": saved.records,
        "bytes": saved.bytes,
    }


@router.get("/api/diagnostics/{name}")
async def download_diagnostics(name: str) -> FileResponse:
    path = diagnostics_bundle.bundle_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="No saved diagnostics file by that name")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name)
