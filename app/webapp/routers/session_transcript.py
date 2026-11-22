"""Coding tab: a live session's full transcript, paginated, plus
one entry's uncapped text.

``GET /api/claude-code/sessions/{sid}/transcript`` returns one page of typed
entries (``user`` / ``assistant`` / ``tool_call`` / ``tool_result`` /
``thinking`` / ``system``) from the session's *native* history, newest-last,
plus a byte-offset cursor for the next older page. Source resolution is the
same as the Board drawer's ``/exchange``: the hook row names a Copilot
``events.jsonl`` when it can, else the file is correlated from the filesystem
by cwd + launch time against Copilot's own per-session ``workspace.yaml``.
A session no source can name answers ``no_transcript`` rather than guessing a
neighbour's file, and an agent this fork cannot launch answers
``unsupported_agent``. None of them read the launcher's PTY capture, so
detached rows are served the same way.
Terminal-grade content, so it sits behind the Tailscale + passkey gate like
``/exchange`` — and so does its sibling below (``middleware``'s
``_TERMINAL_GUARD_RULES`` needs its own row per path shape; ``/transcript``'s
rule matches on a ``.../transcript`` suffix, which a ``/transcript/entry``
request does not, so it is a separate row, not covered for free).

The same path also reads *forwards*: ``?after=<offset>&size=<n>``
returns only what the file has grown by since that offset, which is what the
Chat pane's live refresh ticks on, and answers from one ``stat`` when
``size`` says the file has not grown at all. One path rather than a second
route on purpose — a new path shape needs its own ``_TERMINAL_GUARD_RULES``
row, and this is the same resource, the same caller and the same gate, read
from the other end of the file.

``GET /api/claude-code/sessions/{sid}/transcript/entry?offset=<n>`` re-reads
one ``user``/``assistant`` turn from the same source, uncapped — the Chat
pane's copy button calls it only when the page's own copy of that
entry came back ``truncated: true``. Same source resolution, same gate.

The terminal-screen routes (answering a pending question, the plan picker,
resuming a conversation, context use) live in ``session_screen.py``.

Unavailable sources are told apart on purpose (``reason``): a session the
host doesn't know, an agent with no structured history, a history file that
isn't there, and a file that *is* there but couldn't be read. Bodies are
never logged.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Awaitable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request

from app.webapp.hot_poll import BuildSteps, run_on_default_executor, session_poll
from app.webapp.routers import session_outbox_routes
from app.webapp.routers._helpers import maybe_json
from app.webapp.routers.board_spawn import SESSION_HOST_UNREACHABLE, _read_live_sessions
from src import board, instance_role
from src.launching.vscode_workspace import is_vscode_installed, open_file
from src.middleware import is_pc_itself
from src.session_view import background_tasks as session_tasks
from src.session_view import link_titles as session_link_titles
from src.session_view import links as session_links
from src.session_view.automatic_turns import mark_automatic
from src.session_view.changes import (
    FileRefusal,
    changed_file_path,
    changed_files,
    file_steps,
    read_text_file,
)
from src.session_view.git_changes import select_in_explorer
from src.session_view.session_git import git_changed_path, git_diff_of, merge_git_files
from src.session_view.transcript import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    entry_diff,
    entry_full_text,
    transcript_activity,
    transcript_page,
    transcript_tail,
)
from src.session_view.transcript_locate import find_copilot_transcript
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()
# Files an agent sends to the chat: their own routes, mounted here
# because the transcript responses below carry the outbox alongside.
router.include_router(session_outbox_routes.router)

# Agents whose native history this reader understands. The values are the
# line grammars in `src.session_view.transcript.FLAVORS`; the client's own
# availability list (`session-modes.js`'s `TRANSCRIPT_AGENTS`) mirrors the
# keys, so Chat mode is offered for exactly these agents.
# A byte offset no transcript reaches. A larger one overflows `seek` with an
# OverflowError, which the read routes' OSError handling would not catch.
MAX_OFFSET = 2 ** 62

_FLAVOR_BY_AGENT = {"copilot": "copilot"}


def _unavailable(sid: str, reason: str) -> Dict[str, Any]:
    return {
        "available": False, "source": None, "reason": reason,
        "entries": [], "next_cursor": None, "session_id": sid,
    }


async def _read_or_unavailable(
    sid: str, agent: str, what: str, read: Awaitable[Any],
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Await one on-demand transcript read, turning an ``OSError`` into the
    shared refusal shape instead of leaking it to the caller. Returns
    ``(value, refusal)`` — exactly one of the two is not ``None``. ``read``
    is the already-built awaitable (a ``steps.run(...)`` or
    ``asyncio.to_thread(...)`` call); each route still logs its own success
    breadcrumb, since those fields differ per route."""
    try:
        return await read, None
    except OSError as exc:
        logger.warning("⚠️ %s %s (%s) read failed: %s", what, sid[:8], agent, exc.__class__.__name__)
        return None, _unavailable(sid, "read_failed")


def _resolve_path(session: Dict[str, Any], row: Optional[Dict[str, Any]]) -> Optional[Path]:
    """The history file for this session (None when none is known): the path
    the row carries, else the Copilot event log correlated from the
    filesystem by cwd + launch time against its own per-session
    ``workspace.yaml``."""
    raw = (row or {}).get("transcript_path")
    if raw:
        return Path(str(raw))
    return find_copilot_transcript(session)


async def _resolve_source(
    sid: str, cfg: WebappConfig, steps: Optional[BuildSteps] = None
) -> Tuple[Optional[str], Optional[str], Optional[Path], str, str, Optional[Dict[str, Any]]]:
    """Session lookup + history-path resolution, shared by every route here.

    Returns ``(reason, flavor, path, agent, why, session)`` — ``reason`` is
    set (and ``flavor``/``path`` are ``None``) exactly when the caller must
    respond ``_unavailable(sid, reason)`` instead of reading the source.
    ``why`` is always ``""`` while Copilot is the only flavour: it has no
    refusal guard to explain, but the callers still unpack the slot. ``session``
    is the session-host's row, ``None`` only for ``session_not_found`` and
    ``session_host_unreachable`` (the list could not be read: not the
    same fact as "the session is not in it").

    ``steps`` is the polled ``/transcript`` route's build: its reads
    then run on the per-session pool, off the default executor. Without it
    (an on-demand route) they run on the default executor.
    """
    run = steps.run if steps is not None else run_on_default_executor
    (live, host_error), state = await asyncio.gather(
        run("read_live_sessions", _read_live_sessions, cfg.session_host_port),
        run("read_sessions_state", board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        reason = SESSION_HOST_UNREACHABLE if host_error else "session_not_found"
        return reason, None, None, "", "", None
    agent = str(session.get("agent") or "claude").lower()
    # A detached row resolves exactly like a full-control one: neither
    # source reads the launcher's PTY capture.
    if agent not in _FLAVOR_BY_AGENT:
        return "unsupported_agent", None, None, agent, "", session

    flavor = _FLAVOR_BY_AGENT[agent]
    row = board.state_row_for_session(live, state["rows"], sid)

    def resolve() -> Optional[Path]:
        # The existence check is a stat too, so it stays off the loop.
        found = _resolve_path(session, row)
        return found if found is not None and found.is_file() else None

    path = await run("resolve_path", resolve)
    if path is None:
        return "no_transcript", None, None, agent, "", session
    return None, flavor, path, agent, "", session


@router.get("/api/claude-code/sessions/{sid}/transcript")
async def session_transcript(
    sid: str,
    request: Request,
    before: Optional[int] = Query(default=None, ge=0),
    after: Optional[int] = Query(default=None, ge=0),
    size: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ob: Optional[int] = Query(default=None, ge=0),
) -> Dict[str, Any]:
    """One page of a live session's transcript (Tailscale + passkey).

    ``before`` is the previous page's ``next_cursor`` (a byte offset; omit
    for the newest page); ``limit`` caps the conversation turns per page.

    ``after`` switches the cursor's direction: it asks for only what
    has been appended since that offset, which is what the Chat pane's live
    refresh ticks on. ``size`` is the file size the caller last saw, so an
    unchanged file answers from a single ``stat``. The two directions are
    mutually exclusive — one request reads older or newer, never both.

    Deliberately the **same path** as the backwards page rather than a new
    route. A new path would need its own ``_TERMINAL_GUARD_RULES`` row
    (``/transcript/entry`` is the precedent, and the guard-level test fails any
    session-scoped route with no explicit level) — real work for no gain,
    since this serves the same resource to the same caller under the same
    gate, only from the other end of the file.

    ``ob`` is the outbox rev the caller already holds. Every answer
    with a transcript carries ``outbox_rev``, and the session's sent files
    (``outbox``) only when that rev moved, so the chat sees a new file on
    its next tick without a poll of its own.

    Built through a per-session :class:`app.webapp.hot_poll.HotPoll` keyed
    on every query parameter, so only identical requests share a
    build: past its deadline the answer is that request's last good one,
    with ``stale: true`` and ``age_seconds``.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    if before is not None and after is not None:
        raise HTTPException(
            status_code=400, detail="pass either before or after, not both"
        )
    # The newest page is the chat's opening read and the one page whose
    # bytes still change: its last good copy can be an empty page from when
    # the session was new, shown as "Nothing in the transcript yet" over a
    # long history, so it waits for its build while the chat says Loading.
    # An older page is fixed bytes and a tail read is keyed on its own
    # cursor, so their last good answers stay true.
    poll = session_poll(
        request.app.state, "GET transcript", sid, before, after, size, limit, ob,
        serve_stale=before is not None or after is not None,
    )
    return await poll.get(
        functools.partial(_build_transcript, cfg, sid, before, after, size, limit, ob)
    )


async def _build_transcript(
    cfg: WebappConfig,
    sid: str,
    before: Optional[int],
    after: Optional[int],
    size: Optional[int],
    limit: int,
    ob: Optional[int],
    steps: BuildSteps,
) -> Dict[str, Any]:
    reason, flavor, path, agent, why, session = await _resolve_source(sid, cfg, steps)
    if reason is not None:
        # Neither of these is a source problem, and the live tick asks every
        # few seconds: the unreachable one is already latched by the list read.
        if reason not in ("session_not_found", SESSION_HOST_UNREACHABLE):
            logger.info(
                "ℹ️ transcript %s (%s) unavailable: %s%s",
                sid[:8], agent, reason, f" (refused: {why})" if why else "",
            )
        return _unavailable(sid, reason)
    if after is not None:
        tail = await _tail_response(sid, agent, flavor, path, after, size, session, steps)
        if tail.get("available"):
            tail.update(await steps.run("outbox_fields", session_outbox_routes.outbox_fields, sid, ob))
        return tail
    read_started = time.monotonic()
    page, refusal = await _read_or_unavailable(
        sid, agent, "transcript",
        steps.run("transcript_page", transcript_page, path, before=before, limit=limit, flavor=flavor),
    )
    if refusal is not None:
        return refusal
    await steps.run("mark_automatic", mark_automatic, sid, page["entries"])
    # Size and read time ride the line so a slow or empty page on a long
    # session can be told apart from the logs alone.
    logger.info(
        "ℹ️ transcript %s (%s) page: %d entries, source=%s, more=%s, size=%d, read=%.2fs",
        sid[:8], agent, len(page["entries"]), flavor, page["next_cursor"] is not None,
        page["size"], time.monotonic() - read_started,
    )
    body = {
        "available": True,
        "source": flavor,
        "reason": None,
        "session_id": sid,
        **page,
    }
    if before is None:
        # The newest page is the chat's first read: its activity line starts here.
        body["activity"] = await _activity(flavor, path, steps)
        body["pending_input"] = _pending_input(session)
        body.update(await steps.run("outbox_fields", session_outbox_routes.outbox_fields, sid, ob))
    return body


async def _tail_response(
    sid: str,
    agent: str,
    flavor: str,
    path: Path,
    after: int,
    size: Optional[int],
    session: Optional[Dict[str, Any]],
    steps: BuildSteps,
) -> Dict[str, Any]:
    """The forward-cursor half of the route above.

    Logs nothing on an ordinary tick, on purpose: this runs every few
    seconds for as long as someone is reading a chat, and an info line per
    tick would bury the breadcrumbs that matter under its own traffic. A
    failed read and a reset cursor are rare and diagnostic, so those do log.
    """
    tail, refusal = await _read_or_unavailable(
        sid, agent, "transcript tail",
        steps.run("transcript_tail", transcript_tail, path, after=after, size=size, flavor=flavor),
    )
    if refusal is not None:
        return refusal
    if tail["entries"] or tail["pending"]:
        await steps.run("mark_automatic", mark_automatic, sid, tail["entries"], tail["pending"])
    if tail["reset"]:
        logger.info(
            "ℹ️ transcript tail %s (%s): cursor reset at %d (size %d)",
            sid[:8], agent, after, tail["size"],
        )
    return {
        "available": True,
        "source": flavor,
        "reason": None,
        "session_id": sid,
        **tail,
        "activity": await _activity(flavor, path, steps),
        "pending_input": _pending_input(session),
    }


def _pending_input(session: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """The messages the agent has not taken yet, as the session-host's row
    reports them, for the Chat view's queued chip. Rides the responses the
    chat already polls, like ``activity``. ``None`` when the row does not
    track them (a detached console, or a session-host predating the field),
    which the chip reads as "unknown", not as "nothing pending"."""
    pending = (session or {}).get("pending_input")
    return pending if isinstance(pending, list) else None


async def _activity(flavor: str, path: Path, steps: BuildSteps) -> Optional[Dict[str, Any]]:
    """The chat strip's one-line summary of the turn in progress:
    ``since`` / ``actions`` / ``last`` / ``working``, or ``None`` when the
    transcript cannot say. Rides the page and tail responses — no route or
    poll of its own — and costs a ``stat`` while the file is unchanged.

    Copilot has no harness busy signal, so the turn is read off the
    transcript alone.
    """
    return await steps.run("transcript_activity", transcript_activity, path, flavor=flavor)


@router.get("/api/claude-code/sessions/{sid}/transcript/entry")
async def session_transcript_entry(
    sid: str,
    request: Request,
    offset: int = Query(ge=0),
) -> Dict[str, Any]:
    """One ``user``/``assistant`` turn, uncapped (Tailscale + passkey).

    ``offset`` is the byte offset a page entry carried when the page served
    it ``truncated: true``. ``reason: "entry_not_found"`` covers both a bad
    offset and the ordinary race of the source file moving on (rotated,
    compacted) between the page load and this call — nothing to distinguish
    them by, and the client's fallback (keep the capped text) is the same
    either way.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, _session = await _resolve_source(sid, cfg)
    if reason is not None:
        return _unavailable(sid, reason)
    entry, refusal = await _read_or_unavailable(
        sid, agent, "transcript entry", asyncio.to_thread(entry_full_text, path, offset, flavor),
    )
    if refusal is not None:
        return refusal
    if entry is None:
        logger.info("ℹ️ transcript entry %s (%s) unavailable: entry_not_found", sid[:8], agent)
        return _unavailable(sid, "entry_not_found")
    logger.info(
        "ℹ️ transcript entry %s (%s): %d chars, truncated=%s",
        sid[:8], agent, len(entry["text"]), entry["truncated"],
    )
    return {
        "available": True, "reason": None, "session_id": sid,
        "text": entry["text"], "truncated": entry["truncated"],
    }


@router.get("/api/claude-code/sessions/{sid}/transcript/diff")
async def session_transcript_diff(
    sid: str,
    request: Request,
    offset: int = Query(ge=0),
    n: int = Query(default=0, ge=0, le=999),
) -> Dict[str, Any]:
    """One edit step's whole diff (Tailscale + passkey).

    ``offset`` and ``n`` are the ``diff.n`` ref a page's tool call carried
    when its inline diff came ``truncated``: the line the call was built
    from, and its index among that line's calls. ``reason: "diff_not_found"``
    covers a bad ref and a file that moved on alike — the client keeps the
    inline diff it already has either way.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, _session = await _resolve_source(sid, cfg)
    if reason is not None:
        return _unavailable(sid, reason)
    found, refusal = await _read_or_unavailable(
        sid, agent, "transcript diff", asyncio.to_thread(entry_diff, path, offset, n, flavor),
    )
    if refusal is not None:
        return refusal
    if found is None:
        logger.info("ℹ️ transcript diff %s (%s) unavailable at %d/%d", sid[:8], agent, offset, n)
        return _unavailable(sid, "diff_not_found")
    diff = found["diff"]
    logger.info(
        "ℹ️ transcript diff %s (%s): %d hunks, numbered=%s, truncated=%s",
        sid[:8], agent, len(diff["hunks"]), diff["numbered"], diff["truncated"],
    )
    return {
        "available": True, "reason": None, "session_id": sid,
        "path": found["path"], "diff": diff,
    }


@router.get("/api/claude-code/sessions/{sid}/background-tasks")
async def session_background_tasks(
    sid: str, request: Request, task: Optional[str] = Query(default=None, max_length=40),
) -> Dict[str, Any]:
    """The session's background tasks, or with ``task=<id>`` one task's
    output tail (Tailscale + passkey).

    Folded from the transcript like Changed files, so Claude only:
    ``reason: "unsupported_agent"`` for any other agent, whose ⋮ menu hides
    the item anyway. The output tail is one more query on this path rather
    than a route of its own (the transcript's ``after`` precedent): the
    file it reads is only ever the session's own task file, never a path
    from the request (see :mod:`src.session_view.background_tasks`).
    On demand only; nothing polls it.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, _session = await _resolve_source(sid, cfg)
    if agent != "claude" and reason in (None, "no_transcript"):
        reason = "unsupported_agent"
    if reason is not None:
        logger.info("ℹ️ background tasks %s (%s) unavailable: %s", sid[:8], agent, reason)
        return _unavailable(sid, reason)
    if task is not None:
        found, refusal = await _read_or_unavailable(
            sid, agent, "background task output", asyncio.to_thread(session_tasks.task_output, path, task),
        )
        if refusal is not None:
            return refusal
        if found is None:
            logger.info("ℹ️ background task output %s (%s) unavailable: output_not_found", sid[:8], agent)
            return _unavailable(sid, "output_not_found")
        logger.info("ℹ️ background task output %s (%s): %d bytes, truncated=%s",
                    sid[:8], agent, found["size"], found["truncated"])
        return {"available": True, "reason": None, "session_id": sid, "task": task, **found}
    found, refusal = await _read_or_unavailable(
        sid, agent, "background tasks", asyncio.to_thread(session_tasks.background_tasks, path),
    )
    if refusal is not None:
        return refusal
    logger.info("ℹ️ background tasks %s (%s): %d running, %d finished",
                sid[:8], agent, len(found["running"]), found["finished_total"])
    return {"available": True, "reason": None, "session_id": sid, **found}


def _folded_links_with_titles(path: Any, flavor: str, before: Optional[int] = None) -> Dict[str, Any]:
    """The links fold, each link carrying its page title when one answers."""
    return session_link_titles.attach_titles(session_links.fold_links(path, flavor, before))


@router.get("/api/claude-code/sessions/{sid}/links")
async def session_links_list(
    sid: str, request: Request, before: Optional[int] = Query(default=None, ge=0),
) -> Dict[str, Any]:
    """Every artifact and URL the owner or the agent wrote in this session,
    grouped (Tailscale + passkey). Folded from the transcript like Changed
    files, so it answers after the session ends; every agent with a
    transcript reader. Tool output is not scanned. One request reads one
    window of the transcript; ``before`` is the ``offset`` of the previous
    response and reads the next older window.
    On demand only; nothing polls it.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, _session = await _resolve_source(sid, cfg)
    if reason is not None:
        logger.info("ℹ️ session links %s (%s) unavailable: %s", sid[:8], agent, reason)
        return _unavailable(sid, reason)
    found, refusal = await _read_or_unavailable(
        sid, agent, "session links",
        asyncio.to_thread(_folded_links_with_titles, path, flavor or "copilot", before),
    )
    if refusal is not None:
        return refusal
    logger.info("ℹ️ session links %s (%s): %d links in %d groups, window from byte %d, partial=%s",
                sid[:8], agent, found["total"], len(found["groups"]), found["offset"], found["partial"])
    return {"available": True, "reason": None, "session_id": sid, **found}


def _fold_with_git(path: Path, project_dir: Optional[str], flavor: str, before: Optional[int]) -> Dict[str, Any]:
    """The transcript fold, plus (newest window only) the files git shows the
    session changed that no edit step recorded."""
    found = changed_files(path, project_dir, flavor, before)
    return found if before is not None else merge_git_files(found, project_dir, path)


@router.get("/api/claude-code/sessions/{sid}/changed-files")
async def session_changed_files(
    sid: str, request: Request, before: Optional[int] = Query(default=None, ge=0),
) -> Dict[str, Any]:
    """Every file this session's edits touched (Tailscale + passkey).

    One request reads one window of the transcript: the newest, or the one
    ending at ``before``, the ``next_cursor`` of the response after it (null
    once the start is read), like Session links.

    Folded from the transcript, plus the files ``git status`` shows changed
    since the session began that no edit step recorded (a shell command wrote
    them): those rows say ``source: "git"`` and the ``git`` block names what
    was read — see :mod:`src.session_view.session_git`. Every agent with a transcript reader;
    ``reason: "unsupported_agent"`` for one without, whose ⋮ menu
    hides the item anyway. On demand only, like Show changes: nothing polls
    this.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, session = await _resolve_source(sid, cfg)
    if reason is not None:
        logger.info("ℹ️ changed files %s (%s) unavailable: %s", sid[:8], agent, reason)
        return _unavailable(sid, reason)
    project_dir = str((session or {}).get("project_dir") or "") or None
    found, refusal = await _read_or_unavailable(
        sid, agent, "changed files", asyncio.to_thread(_fold_with_git, path, project_dir, flavor, before),
    )
    if refusal is not None:
        return refusal
    logger.info(
        "ℹ️ changed files %s (%s): %d files, +%d -%d, window from byte %s, partial=%s, project_exists=%s, git=%s",
        sid[:8], agent, len(found["files"]), found["counts"]["additions"],
        found["counts"]["deletions"], found["next_cursor"] or 0, found["partial"], found["project_exists"],
        (found.get("git") or {}).get("state", "not read"),
    )
    return {
        "available": True, "reason": None, "session_id": sid,
        "source": "transcript", **found,
    }


@router.get("/api/claude-code/sessions/{sid}/changed-files/diff")
async def session_changed_file_diff(
    sid: str, request: Request, path: str = Query(min_length=1),
    since: Optional[int] = Query(default=None, ge=0),
) -> Dict[str, Any]:
    """One changed file's diff: each of this session's edits to it, in
    order (Tailscale + passkey). ``path`` is a ``key`` the list
    handed out; ``since`` the oldest ``next_cursor`` the panel loaded; a file the session never edited is ``file_not_found`` —
    nothing here reads the file system, only the transcript."""
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, source, agent, _why, session = await _resolve_source(sid, cfg)
    if reason is not None:
        return _unavailable(sid, reason)
    project_dir = str((session or {}).get("project_dir") or "") or None
    found, refusal = await _read_or_unavailable(
        sid, agent, "changed file diff",
        asyncio.to_thread(file_steps, source, path, flavor=flavor, project_dir=project_dir, since=since),
    )
    if refusal is not None:
        return refusal
    if found is None:
        git_found, refusal = await _read_or_unavailable(
            sid, agent, "changed file git diff", asyncio.to_thread(git_diff_of, project_dir, source, path),
        )
        if refusal is not None:
            return refusal
        if git_found is not None:
            logger.info("ℹ️ changed file diff %s (%s): from git status", sid[:8], agent)
            return {"available": True, "reason": None, "session_id": sid, "steps": [], "partial": False,
                    "git_diff": git_found["diff"], "binary": git_found["binary"],
                    "truncated": git_found["truncated"]}
        logger.info("ℹ️ changed file diff %s (%s): file_not_found", sid[:8], agent)
        return _unavailable(sid, "file_not_found")
    logger.info(
        "ℹ️ changed file diff %s (%s): %d steps, truncated=%s",
        sid[:8], agent, len(found["steps"]), found["truncated"],
    )
    return {"available": True, "reason": None, "session_id": sid, **found}


async def _changed_file_target(
    sid: str, request: Request, requested: str, what: str, since: Optional[int] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """``(target, refusal, agent)`` for a file the panel names: the absolute
    path when this session's edits touched it, else the unavailable body to
    answer with. The file-system jail of the two routes below."""
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, source, agent, _why, session = await _resolve_source(sid, cfg)
    if reason is not None:
        logger.info("ℹ️ changed file %s %s (%s) unavailable: %s", what, sid[:8], agent, reason)
        return None, _unavailable(sid, reason), agent
    project_dir = str((session or {}).get("project_dir") or "") or None
    target, refusal = await _read_or_unavailable(
        sid, agent, f"changed file {what}",
        asyncio.to_thread(changed_file_path, source, requested, flavor=flavor, project_dir=project_dir, since=since),
    )
    if refusal is not None:
        return None, refusal, agent
    if target is None:
        target, refusal = await _read_or_unavailable(
            sid, agent, f"changed file {what} (git)",
            asyncio.to_thread(git_changed_path, project_dir, source, requested),
        )
        if refusal is not None:
            return None, refusal, agent
    if target is None:
        logger.info("ℹ️ changed file %s %s (%s): file_not_found", what, sid[:8], agent)
        return None, _unavailable(sid, "file_not_found"), agent
    return target, None, agent


@router.get("/api/claude-code/sessions/{sid}/changed-files/content")
async def session_changed_file_content(
    sid: str, request: Request, path: str = Query(min_length=1),
    since: Optional[int] = Query(default=None, ge=0),
) -> Dict[str, Any]:
    """One changed file as it is on disk now, for the panel's File view
    (Tailscale + passkey).

    Only a file this session's edits touched is read: ``path`` is looked up
    in the transcript's fold, never joined and opened, so any other path is
    ``file_not_found``. Then each reason it can't be shown is its own:
    ``deleted``, ``too_large`` (over 256 KB, with ``size``), ``not_text``
    and ``read_failed``. The text is never logged, nor the path.
    """
    target, refusal, agent = await _changed_file_target(sid, request, path, "content", since)
    if refusal is not None:
        return refusal
    try:
        text, refusal = await _read_or_unavailable(
            sid, agent, "changed file content", asyncio.to_thread(read_text_file, target),
        )
    except FileRefusal as exc:
        logger.info("ℹ️ changed file content %s (%s): %s", sid[:8], agent, exc.reason)
        body = _unavailable(sid, exc.reason)
        if exc.size is not None:
            body["size"] = exc.size
        return body
    if refusal is not None:
        return refusal
    logger.info("ℹ️ changed file content %s (%s): %d chars", sid[:8], agent, len(text))
    return {
        "available": True, "reason": None, "session_id": sid,
        "path": str(PureWindowsPath(target)), "text": text,
    }


@router.post("/api/claude-code/sessions/{sid}/changed-files/open")
async def session_changed_file_open(
    sid: str, request: Request, since: Optional[int] = Query(default=None, ge=0),
) -> Dict[str, Any]:
    """Open one changed file on the PC: in VS Code, else selected in
    Explorer — never the OS default handler, which would run a script.

    Only the PC itself may ask, like ``/api/open-url``: from the phone the
    editor would open in front of nobody. Same jail as the content route; a
    file gone from disk is ``deleted``. A disposable e2e / verify instance
    launches nothing and answers ``opened: false``.
    """
    host = request.client.host if request.client else ""
    if not is_pc_itself(host, request.headers):
        raise HTTPException(status_code=403, detail="files open on the PC only for the PC itself")
    body = await maybe_json(request)
    requested = str((body or {}).get("path") or "").strip()
    if not requested:
        raise HTTPException(status_code=400, detail="missing path")
    target, refusal, agent = await _changed_file_target(sid, request, requested, "open", since)
    if refusal is not None:
        return {**refusal, "opened": False}
    if not await asyncio.to_thread(os.path.isfile, target):
        logger.info("ℹ️ changed file open %s (%s): deleted", sid[:8], agent)
        return {**_unavailable(sid, "deleted"), "opened": False}
    if instance_role.is_disposable():
        logger.info("ℹ️ changed file open %s (%s): disposable instance, not opening", sid[:8], agent)
        return {"available": True, "reason": None, "session_id": sid, "opened": False, "with": "none"}
    opener = "vscode"
    try:
        if not is_vscode_installed():
            raise OSError("the 'code' CLI is not on PATH")
        await asyncio.to_thread(open_file, Path(target))
    except OSError as exc:
        logger.info("ℹ️ changed file open %s (%s): VS Code unavailable (%s), Explorer instead",
                    sid[:8], agent, exc)
        opener = "explorer"
        try:
            await asyncio.to_thread(select_in_explorer, Path(target))
        except OSError as exc2:
            logger.warning("⚠️ changed file open %s (%s) failed: %s", sid[:8], agent, exc2)
            raise HTTPException(status_code=503, detail="could not open the file on the PC") from exc2
    logger.info("ℹ️ changed file open %s (%s): opened with %s", sid[:8], agent, opener)
    return {"available": True, "reason": None, "session_id": sid, "opened": True, "with": opener}
