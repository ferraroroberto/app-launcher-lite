"""Artifacts, pinning, and live output for Jobs-tab run records."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketDisconnect

from app.webapp.middleware import LOOPBACK_HOSTS
from app.webapp.routers._helpers import maybe_json
from src import api_tokens
from src import jobs as jobs_mod
from src.jobs.config import get_by_id, load_jobs
from src.jobs.run_state import STATUS_PENDING, run_view

router = APIRouter()


def _known_run_dir(job_id: str, run_id: str) -> Optional[Path]:
    """Return a registered job's existing run dir, else ``None``."""
    if get_by_id(load_jobs(), job_id) is None:
        return None
    run_dir = jobs_mod.runs_dir(job_id) / run_id
    return run_dir if run_dir.is_dir() else None


@router.put("/api/jobs/{job_id}/runs/{run_id}")
async def update_job_run(job_id: str, run_id: str, request: Request) -> dict:
    """Update the keep-forever flag on one canonical run record."""
    run_dir = _known_run_dir(job_id, run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="unknown job or run")
    body = await maybe_json(request)
    if set(body) != {"pinned"} or not isinstance(body.get("pinned"), bool):
        raise HTTPException(status_code=400, detail="body must be {pinned: bool}")
    await asyncio.to_thread(jobs_mod.write_run_json, run_dir, pinned=body["pinned"])
    record = await asyncio.to_thread(jobs_mod.read_run, run_dir)
    record.setdefault("run_id", run_id)
    return {"run": record}


@router.get("/api/jobs/{job_id}/runs/{run_id}/artifacts/{filename:path}")
async def download_job_artifact(job_id: str, run_id: str, filename: str) -> FileResponse:
    """Serve one artifact while keeping resolution strictly inside its run."""
    run_dir = _known_run_dir(job_id, run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="unknown job or run")
    base = (run_dir / "artifacts").resolve()
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid artifact path") from exc
    # The API contract is a filename, not an arbitrary nested path. Keeping
    # direct children only makes the jail obvious and matches list_artifacts.
    if candidate.parent != base:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(candidate, filename=candidate.name)


@router.websocket("/api/jobs/{job_id}/runs/{run_id}/stream")
async def stream_job_run(websocket: WebSocket, job_id: str, run_id: str) -> None:
    """Send one output snapshot, incremental chunks, then final status."""
    await websocket.accept()
    cfg = websocket.app.state.webapp_config
    client_host = websocket.client.host if websocket.client else ""
    if client_host not in LOOPBACK_HOSTS and not api_tokens.full_scope_credential_ok(
        websocket.query_params.get("token", "").strip(),
        cfg.auth_token,
        getattr(cfg, "api_tokens", None) or [],
    ):
        await websocket.close(code=4401, reason="missing or invalid bearer token")
        return
    run_dir = _known_run_dir(job_id, run_id)
    if run_dir is None:
        await websocket.close(code=4404, reason="unknown job or run")
        return

    output_path = run_dir / "output.log"
    offset = 0
    first = True
    try:
        while True:
            record = await asyncio.to_thread(jobs_mod.read_run, run_dir)
            # run.json lands just after the run dir; until then it is pending.
            record = record or {"status": STATUS_PENDING}
            view = run_view(record, now=datetime.now())
            status = str(record.get("status") or STATUS_PENDING)
            try:
                size = output_path.stat().st_size if output_path.is_file() else 0
            except OSError:
                size = 0
            if first or size < offset:
                snapshot = await asyncio.to_thread(jobs_mod.read_output_tail, run_dir)
                await websocket.send_json(
                    {
                        "type": "snapshot",
                        "output": snapshot,
                        "status": status,
                        "live": view["live"],
                    }
                )
                offset = size
                first = False
            elif size > offset:
                length = size - offset

                def _read_delta() -> bytes:
                    with output_path.open("rb") as handle:
                        handle.seek(offset)
                        return handle.read(length)

                delta = await asyncio.to_thread(_read_delta)
                offset = size
                if delta:
                    await websocket.send_json(
                        {
                            "type": "chunk",
                            "output": delta.decode("utf-8", errors="replace"),
                            "status": status,
                            "live": view["live"],
                        }
                    )
            if not view["open"]:
                await websocket.send_json(
                    {"type": "status", "status": status, "live": view["live"]}
                )
                await websocket.close(code=1000)
                return
            await asyncio.sleep(0.1)
    except (OSError, RuntimeError, WebSocketDisconnect):
        return
