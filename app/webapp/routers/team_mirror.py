"""Team Mirror: publish a project to its team repo as one snapshot commit.

``GET    /api/team-mirror``                     -> ``{identity, projects: [{project, link, status}]}``
``PUT    /api/team-mirror/links/{project}``     -> save the link (``team_url``, ``branch``, ``leave_out``)
``DELETE /api/team-mirror/links/{project}``     -> remove it
``POST   /api/team-mirror/test-access``         -> ``{ok, error}`` from a read-only ``ls-remote``
``POST   /api/team-mirror/{project}/preview``   -> what a publish would do, nothing written
``POST   /api/team-mirror/{project}/publish``   -> ``{result, ...}`` (``mode``, ``confirm_team_changes``, ``pr_body``)

The list serves each linked project's status from a 30-minute cache unless
``?refresh=1``; every other call works it out afresh. Links are saved to the
machine-local webapp config, never to either repo. Everything but the list is
passkey-gated (``src/middleware/routes.py``): a publish pushes with this
computer's git sign-in.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.webapp.route_pools import OUTBOUND, run_on
from app.webapp.routers._helpers import maybe_json
from src.scanner import scan_project_dirs
from src.team_mirror import forge, links, publish, scan, snapshot
from src.webapp_config import WebappConfig, update_webapp_config

logger = logging.getLogger(__name__)
router = APIRouter()


def _project_dirs(cfg: WebappConfig) -> Dict[str, Path]:
    projects = scan_project_dirs(Path(cfg.projects_dir), list(cfg.projects_ignore))
    return {p.name: p.project_dir for p in projects if (p.project_dir / ".git").exists()}


def _context(cfg: WebappConfig, project: str, project_dir: Path, link: links.MirrorLink) -> publish.Context:
    return publish.Context(project, project_dir, link, scan.clean_terms(cfg.mirror_scan_terms), cfg.llm_hub_url)


async def _linked(request: Request, project: str) -> publish.Context:
    cfg: WebappConfig = request.app.state.webapp_config
    dirs = await run_on(OUTBOUND, _project_dirs, cfg)
    if project not in dirs:
        raise HTTPException(status_code=404, detail="no such app in the projects folder")
    link = links.parse_links(cfg.team_mirrors).get(project)
    if link is None:
        raise HTTPException(status_code=404, detail="this app has no team repo linked")
    return _context(cfg, project, dirs[project], link)


@router.get("/api/team-mirror")
async def list_mirrors(request: Request, refresh: bool = False) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    dirs = await run_on(OUTBOUND, _project_dirs, cfg)
    saved = links.parse_links(cfg.team_mirrors)
    linked = [name for name in dirs if name in saved]
    statuses = await asyncio.gather(*(
        run_on(OUTBOUND, publish.cached_status, _context(cfg, name, dirs[name], saved[name]), refresh=refresh)
        for name in linked
    ))
    by_name = dict(zip(linked, statuses, strict=True))
    rows: List[Dict[str, Any]] = []
    for name in dirs:
        link: Optional[links.MirrorLink] = saved.get(name)
        rows.append({
            "project": name,
            "link": link.as_row() if link else None,
            "host": forge.remote_of(link.team_url).kind if link else None,
            "status": by_name.get(name, {"project": name, "state": "not_linked"}),
        })
    identity = await run_on(OUTBOUND, links.work_identity, Path(cfg.projects_dir))
    return {
        "identity": identity.__dict__ if identity else None,
        "refresh_minutes": publish.STATUS_TTL_SECONDS // 60,
        "projects": rows,
    }


@router.put("/api/team-mirror/links/{project}")
async def save_link(project: str, request: Request) -> Any:
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    dirs = await run_on(OUTBOUND, _project_dirs, cfg)
    if project not in dirs:
        raise HTTPException(status_code=404, detail="no such app in the projects folder")
    try:
        link = links.checked_link({**body, "project": project})
    except links.LinkError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc), "field": exc.field})
    request.app.state.webapp_config = update_webapp_config(team_mirrors=links.with_link(cfg.team_mirrors, link))
    publish.forget_status(project)
    logger.info("ℹ️ team mirror: saved the link for %s", project)
    return {"ok": True, "link": link.as_row()}


@router.delete("/api/team-mirror/links/{project}")
async def delete_link(project: str, request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    request.app.state.webapp_config = update_webapp_config(team_mirrors=links.without_link(cfg.team_mirrors, project))
    publish.forget_status(project)
    logger.info("ℹ️ team mirror: removed the link for %s", project)
    return {"ok": True}


@router.post("/api/team-mirror/test-access")
async def test_access(request: Request) -> Any:
    body = await maybe_json(request)
    try:
        link = links.checked_link({**body, "project": "access-probe"})
    except links.LinkError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc), "field": exc.field})

    def probe() -> Optional[str]:
        repo = snapshot.scratch_repo("access-probe")
        return snapshot.check_access(repo, link.team_url, link.branch)

    try:
        problem = await run_on(OUTBOUND, probe)
    except snapshot.SnapshotError as exc:
        problem = str(exc)
    return {"ok": problem is None, "error": problem}


@router.post("/api/team-mirror/{project}/preview")
async def preview(project: str, request: Request) -> Dict[str, Any]:
    ctx = await _linked(request, project)
    try:
        return await run_on(OUTBOUND, publish.preview, ctx)
    except snapshot.SnapshotError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/team-mirror/{project}/publish")
async def publish_project(project: str, request: Request) -> Dict[str, Any]:
    ctx = await _linked(request, project)
    body = await maybe_json(request)
    mode = str(body.get("mode") or publish.MODE_MERGE)
    if mode not in publish.MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(publish.MODES)}")
    pr_body = body.get("pr_body")
    try:
        return await run_on(
            OUTBOUND, publish.publish, ctx, mode=mode,
            confirm_team_changes=body.get("confirm_team_changes") is True,
            pr_body=pr_body if isinstance(pr_body, str) else None,
        )
    except publish.PublishBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (snapshot.SnapshotError, forge.ForgeError) as exc:
        logger.warning("⚠️ team mirror: publish of %s failed: %s", project, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
