"""Settings > Diagnostics: save the last N minutes of every Lite log to one file.

``POST /api/diagnostics``          ``{minutes}`` -> ``{name, path, records, bytes}``
``GET  /api/diagnostics/{name}``   -> that saved file, as plain text

The file is written on this PC by :mod:`src.diagnostics_bundle`, redacted
before it is written, and never sent anywhere; the GET is what lets the phone
download it. Both are passkey-gated (``src/middleware/routes.py``): the file
holds session lifecycle and chief traffic, terminal-grade content.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src import diagnostics_bundle
from src.build_info import build_identity
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
router = APIRouter()

#: The build this process loaded, named in the file beside the checkout's.
_SERVED_SHA = build_identity().get("git_sha", "unknown")


class SaveRequest(BaseModel):
    minutes: int = 60


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
