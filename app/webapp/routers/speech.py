"""Speech to text: the dictation upload proxy and the Settings connection test.

Every record button posts its take to ``/api/transcribe``; the webapp forwards
it to the OpenAI-shaped endpoint set in Settings > Speech to text, converted
to 16 kHz mono WAV when it arrives in another shape (``speech_audio``). With the
switch off the route answers 503 and no record button is shown.

Beside that upload, each take is also streamed to ``/api/transcribe/recordings``
while it is recorded (``src/recordings.py``), so a crashed page or a failed
transcription leaves the audio on disk for Settings to replay and transcribe
again. Those routes sit under ``/api/transcribe/`` to share its passkey gate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.webapp.routers._helpers import audit_off_loop, client_ip
from src import audit, recordings
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


def _keep_hours(request: Request) -> int:
    cfg: WebappConfig = request.app.state.webapp_config
    return cfg.speech_keep_recordings_hours


def _http(exc: recordings.RecordingError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/api/transcribe/recordings")
def recording_create(request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Start keeping a take on disk: ``{"id"}``, ``null`` while the setting is 0."""
    _endpoint(request)
    mime = str((body or {}).get("mime") or "audio/webm")
    return {"id": recordings.create(mime, _keep_hours(request))}


@router.post("/api/transcribe/recordings/{take_id}/chunk")
def recording_chunk(
    take_id: str, ms: int = 0, data: bytes = Body(..., media_type="application/octet-stream"),
) -> Dict[str, Any]:
    """Append one recorder chunk as it came, never re-encoded."""
    if not data:
        raise HTTPException(status_code=400, detail="empty chunk")
    try:
        return {"bytes": recordings.append(take_id, data, ms)}
    except recordings.RecordingError as exc:
        raise _http(exc)


@router.post("/api/transcribe/recordings/{take_id}/finish")
def recording_finish(take_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Record how the take ended: ``{"status": stopped|transcribed|failed, "error"}``."""
    try:
        recordings.set_status(take_id, str(body.get("status", "")), str(body.get("error") or ""))
    except recordings.RecordingError as exc:
        raise _http(exc)
    return {"ok": True}


@router.get("/api/transcribe/recordings")
def recording_list() -> Dict[str, Any]:
    """Settings > Speech to text's Recent recordings, newest first."""
    return {"recordings": recordings.list_takes()}


@router.get("/api/transcribe/recordings/{take_id}/audio")
def recording_audio(take_id: str) -> FileResponse:
    """The take's audio as recorded, for the Settings player."""
    try:
        path = recordings.audio_path(take_id)
        mime = recordings.mime_of(take_id)
    except recordings.RecordingError as exc:
        raise _http(exc)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no audio saved for this recording")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-store"})


def _transcribe_kept(cfg: WebappConfig, take_id: str) -> Tuple[Dict[str, Any], int]:
    """Send a kept take to the speech endpoint and record the outcome on it."""
    try:
        path = recordings.audio_path(take_id)
        mime = recordings.mime_of(take_id)
    except recordings.RecordingError as exc:
        raise _http(exc)
    content = path.read_bytes() if path.is_file() else b""
    if not content:
        raise HTTPException(status_code=400, detail="empty recording")
    try:
        result = speech_client.transcribe_upload(
            cfg.speech_base_url, cfg.speech_model, cfg.speech_api_key,
            path.name, content, mime,
        )
    except speech_client.SpeechError as exc:
        logger.warning("⚠️ kept recording transcription failed (HTTP %s)", exc.status)
        recordings.set_status(take_id, recordings.STATUS_FAILED, str(exc))
        raise HTTPException(status_code=exc.status, detail=str(exc))
    recordings.set_status(take_id, recordings.STATUS_TRANSCRIBED)
    return result, len(content)


@router.post("/api/transcribe/recordings/{take_id}/transcribe")
async def recording_transcribe(request: Request, take_id: str) -> Dict[str, Any]:
    """Transcribe a kept take again, on the worker pool the sync routes share."""
    cfg = _endpoint(request)
    result, size = await run_in_threadpool(_transcribe_kept, cfg, take_id)
    await audit_off_loop(
        audit.audit_event, "transcribe", bytes=size, silent=bool(result.get("silent")),
        client=client_ip(request), kept=True,
    )
    return result
