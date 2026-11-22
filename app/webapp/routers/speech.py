"""Speech to text: the dictation upload proxy and the Settings connection test.

Every record button posts its take to ``/api/transcribe``; the webapp forwards
it to the OpenAI-shaped endpoint set in Settings > Speech to text, converted
to 16 kHz mono WAV when it arrives in another shape (``speech_audio``). With the
switch off the route answers 503 and no record button is shown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.webapp.routers._helpers import audit_off_loop, client_ip
from src import audit
from src.clients import speech as speech_client
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
router = APIRouter()


def _endpoint(request: Request) -> WebappConfig:
    """The live config when dictation is on and has an endpoint, else a 503."""
    cfg: WebappConfig = request.app.state.webapp_config
    if not cfg.speech_enabled or not cfg.speech_base_url:
        raise HTTPException(status_code=503, detail="speech to text is off in Settings")
    return cfg


@router.post("/api/transcribe")
async def transcribe(request: Request, file: UploadFile = File(...)) -> Dict[str, Any]:
    """Transcribe one dictation take; the transcript lands in the composer for review."""
    cfg = _endpoint(request)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty recording")
    try:
        result = await asyncio.to_thread(
            speech_client.transcribe_upload,
            cfg.speech_base_url,
            cfg.speech_model,
            cfg.speech_api_key,
            file.filename or "recording.webm",
            content,
            file.content_type or "audio/webm",
        )
    except speech_client.SpeechError as exc:
        logger.warning("⚠️ transcription failed (HTTP %s): %s", exc.status, exc)
        raise HTTPException(status_code=exc.status, detail=str(exc))
    await audit_off_loop(
        audit.audit_event,
        "transcribe",
        bytes=len(content),
        silent=bool(result.get("silent")),
        client=client_ip(request),
    )
    return result


@router.post("/api/speech/test")
async def speech_test(request: Request) -> Dict[str, Any]:
    """Probe the saved endpoint with a short test recording: ``{"state", "detail"}``,
    the state one of transcribes, refuses_audio, reachable, refused or unknown.

    Reads only the saved settings, never a URL from the request, so this route
    cannot be pointed at an arbitrary host.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    if not cfg.speech_base_url:
        raise HTTPException(status_code=400, detail="Set a base URL first.")
    return await asyncio.to_thread(
        speech_client.check_connection, cfg.speech_base_url, cfg.speech_model, cfg.speech_api_key
    )
