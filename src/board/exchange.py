"""Conversation previews for the Board drawer.

The hook row's transcript is the best source when it exists because it is
structured chat data; for a Copilot session without one, its ``events.jsonl``
is correlated from the filesystem instead. Launcher-owned PTYs also have an
exact-id capture, which is the fallback when no structured history answers.

The capture is terminal output, not prose.  A bounded tail is replayed through
``pyte`` and reply blocks are selected by the same leading-bullet colour
contract as the browser's read-aloud extractor.  This keeps ANSI/full-screen
repaint bytes out of the API response and avoids any cwd-based guessing.

The filesystem correlation itself — locating the Copilot event log that
belongs to a live session — lives in ``src/session_view/transcript_locate.py``;
this module resolves *what to show*, not *which file*.
"""

from __future__ import annotations

import ast
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pyte

from src.board.transcript import _read_tail_bytes, _tail_lines
from src.board.transcript_exchange import last_exchange
from src.session_host_scan import _cook_input_line
from src.session_view import capture_screen, transcript_locate
from src.transcript_flavors.copilot import copilot_entries

logger = logging.getLogger(__name__)

_CAPTURE_TAIL_BYTES = 512 * 1024
# The launcher input log (``webapp/sessions/<sid>.log``) is appended one
# ``[input]`` line per chunk for a session's whole life, with no rotation, and
# the chief drawer re-polls it every 5s — so only its tail is read, like
# every other reader here. Sized by measurement against the 12 largest logs
# on this box: some sessions log 100+ KB of non-submitting input (keys,
# terminal escape traffic) after their last Enter, so 64 KB changed the shown
# prompt on 3 of 12 while 256 KB matched a full read on all 12 — the same
# window as ``src.board.transcript._EXCHANGE_TAIL_BYTES``. A submission older than
# the window falls back to the session's prompt title.
_INPUT_TAIL_BYTES = 256 * 1024
_CAPTURE_HISTORY_LINES = 2500
# Copilot's TUI asks ``CSI ? Ps n`` (private-mode device status); pyte's
# ``report_device_status`` takes no ``private`` argument and raises, and a
# replay has no terminal to answer, so the query is dropped before feeding.
_PRIVATE_DSR = re.compile(r"\x1b\[\?[0-9;]*n")
_EVENTS_TAIL_BYTES = 4 * 1024 * 1024
_ASSISTANT_TEXT_CAP = 6000
_USER_TEXT_CAP = 1500

_BULLETS = frozenset({"●", "⏺", "•", "◉", "○"})
_SATURATED_NAMED = frozenset({
    "black", "red", "green", "brown", "blue", "magenta", "cyan",
    "brightblack", "brightred", "brightgreen", "brightblue",
    "brightmagenta", "brightcyan",
})
# Rendered capture rows, keyed by capture path. The ``pyte`` replay of
# the 512 KB tail is ~0.5-1 s of pure-Python CPU (measured on the dev box's
# largest captures) and the fleet chief reads every lane's exchange on every
# wake, while an idle session writes nothing, so an unchanged capture is
# answered from here and a tail renders once per change. The fingerprint is
# ``(size, mtime_ns, rows, cols)`` taken *before* the read: the capture only
# grows, so a write landing after the stat leaves the cached rows keyed to an
# older fingerprint and the next call re-renders - never the reverse.
_ROWS_CACHE_MAX = 64
_ROWS_CACHE: Dict[str, Tuple[Tuple[int, int, int, int], List[Tuple[str, str]]]] = {}
_ROWS_CACHE_LOCK = threading.Lock()
_LEAD_BULLET_RE = re.compile(r"^[●⏺•◉○]\s+")
_TOOL_CALL_RE = re.compile(r"^[●⏺•◉○]\s+[A-Z][A-Za-z0-9_-]*\(")
_RULE_RUN_RE = re.compile(r"[─━═┄┅┈┉╌╍]{6,}")
_RULE_CHARS_RE = re.compile(r"[─-▟│┄┅┈┉╌╍]")
_TIMING_RE = re.compile(
    r"^\s*[*✶✻✽✢✱·•∗⁘]?\s*[A-Z][a-z]+ for \d+\s*[smhd]\b"
)
_SPINNER_RE = re.compile(
    r"^\s*[*✶✻✽✢✱·•∗⁘]?\s*[A-Z][a-z]+(?:…|\.\.\.)\s*\("
)
_TIP_RE = re.compile(r"^\s*[⎿└╰⤷↳]\s*Tip\b", re.IGNORECASE)
_INPUT_RE = re.compile(r"\[input\]\s+(.*)$")


def unavailable(reason: str) -> Dict[str, Any]:
    """Canonical unavailable response with a machine-readable reason."""
    return {
        "available": False,
        "source": None,
        "reason": reason,
        "user": None,
        "assistant": None,
        "latest_user": None,
    }


def _turn_time(timestamp: Any) -> Optional[Dict[str, Any]]:
    """``{"timestamp", "at"}`` for a turn's ISO-8601 time, ``at`` in epoch
    seconds so it compares with the session-host's ``written_at``; None when
    there is no readable time."""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return {"timestamp": timestamp, "at": at}


def resolve_exchange(
    session: Dict[str, Any],
    native_path: Any,
    launcher_capture_path: Path,
    launcher_input_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve one live session's exchange through the source hierarchy.

    The row-declared ``native_path`` is exact and always wins. A Copilot
    session whose row names none has its ``events.jsonl`` found from the
    filesystem instead. Anything else falls back to the launcher's exact-id
    PTY capture, so a session whose structured history cannot be read
    degrades to the rougher-but-honest answer rather than to a neighbour's
    text.
    """
    native = last_exchange(native_path)
    if native.get("available"):
        return {"latest_user": None, **native, "source": "native", "reason": None}

    latest_user = None
    agent = str(session.get("agent") or "").lower()
    if agent == "copilot":
        copilot = _copilot_session_exchange(session, native_path)
        if copilot.get("available"):
            return copilot
        # A prompt the agent has not answered yet is still a turn the caller
        # may be waiting to see, so its time survives the fallback below.
        latest_user = copilot.get("latest_user")
    return {
        **_fallback_exchange(session, native_path, launcher_capture_path, launcher_input_path),
        "latest_user": latest_user,
    }


def _fallback_exchange(
    session: Dict[str, Any],
    native_path: Any,
    launcher_capture_path: Path,
    launcher_input_path: Optional[Path],
) -> Dict[str, Any]:
    """The launcher capture's exchange, else the unavailable answer naming why."""
    fallback = launcher_last_exchange(
        launcher_capture_path,
        launcher_input_path=launcher_input_path,
        prompt_fallback=str(session.get("prompt_title") or "").strip(),
        rows=int(session.get("rows") or 40),
        cols=int(session.get("cols") or 120),
    )
    if fallback.get("available"):
        return fallback

    reason = _unavailable_reason(fallback, native_path, launcher_capture_path)
    return unavailable(reason)


def _copilot_session_exchange(session: Dict[str, Any], native_path: Any) -> Dict[str, Any]:
    """A Copilot session's exchange from its ``events.jsonl``.

    The hook-named path wins when a row carries one, else the launch-window
    match finds the file.
    """
    path = (
        Path(str(native_path)) if native_path
        else transcript_locate.find_copilot_transcript(session)
    )
    return copilot_last_exchange(path)


def _unavailable_reason(fallback: Dict[str, Any], native_path: Any, launcher_capture_path: Path) -> str:
    """Why no source answered: the capture's own reason, else the gap."""
    if _nonempty_file(launcher_capture_path):
        return str(fallback.get("reason") or "capture_unparseable")
    if native_path:
        return "native_unavailable"
    return "no_exchange"


def copilot_last_exchange(path: Optional[Path]) -> Dict[str, Any]:
    """Read the newest Copilot user/assistant messages from ``events.jsonl``.

    Goes through :func:`transcript_flavors.copilot.copilot_entries` so a turn
    whose only text is autopilot's ``task_complete`` summary still has a reply.
    The log is Copilot's own structured record, so the answer is ``native``.
    """
    return _flavor_last_exchange(path, copilot_entries, "native")


def _flavor_last_exchange(
    path: Optional[Path], entries_of: Callable[..., List[Dict[str, Any]]], source: str,
) -> Dict[str, Any]:
    if path is None:
        return unavailable("native_unavailable")
    raw = _read_tail(path, _EVENTS_TAIL_BYTES)
    if not raw:
        return unavailable("native_unavailable")
    entries = entries_of(list(enumerate(raw.splitlines())), uncapped=True)
    messages = [e for e in entries if e["kind"] in ("user", "assistant")]
    # The newest user turn, answered or not: what a caller matches its own
    # input's acknowledged write time against.
    latest_user = next(
        (_turn_time(m.get("timestamp")) for m in reversed(messages) if m["kind"] == "user"),
        None,
    )
    assistant_index = next(
        (index for index in range(len(messages) - 1, -1, -1)
         if messages[index]["kind"] == "assistant"),
        None,
    )
    if assistant_index is None:
        return {**unavailable("no_exchange"), "latest_user": latest_user}
    user_record = next(
        (messages[index] for index in range(assistant_index - 1, -1, -1)
         if messages[index]["kind"] == "user"),
        None,
    )
    assistant = messages[assistant_index]
    return {
        "available": True,
        "source": source,
        "reason": None,
        "user": (
            {
                "text": user_record["text"][-_USER_TEXT_CAP:],
                "timestamp": user_record["timestamp"],
            }
            if user_record else None
        ),
        "assistant": {
            "text": assistant["text"][-_ASSISTANT_TEXT_CAP:],
            "timestamp": assistant["timestamp"],
        },
        "latest_user": latest_user,
    }


def launcher_last_exchange(
    capture_path: Path,
    *,
    launcher_input_path: Optional[Path] = None,
    prompt_fallback: str = "",
    rows: int = 40,
    cols: int = 120,
) -> Dict[str, Any]:
    """Extract the latest reply from an exact-id PTY capture tail.

    The tail is read through :func:`capture_screen.read_capture_tail`, which undoes
    the session-host's text-mode CRLF expansion and starts the render after the
    first newline; only the scrollback-aware, colour-reading render below is
    local, because the shared ``screen_lines`` keeps neither history nor cells.
    """
    rows, cols = max(2, rows), max(20, cols)
    try:
        st = Path(capture_path).stat()
        fingerprint: Optional[Tuple[int, int, int, int]] = (
            st.st_size, st.st_mtime_ns, rows, cols
        )
    except OSError:
        fingerprint = None
    parsed_rows = _cached_rows(capture_path, fingerprint)
    if parsed_rows is None:
        raw = capture_screen.read_capture_tail(capture_path, _CAPTURE_TAIL_BYTES)
        if not raw:
            return unavailable("no_exchange")
        try:
            parsed_rows = _terminal_rows(raw, rows=rows, cols=cols)
        except Exception:
            logger.warning(
                "⚠️ exchange: capture %s did not parse; answering capture_unparseable",
                capture_path, exc_info=True,
            )
            return unavailable("capture_unparseable")
        _store_rows(capture_path, fingerprint, parsed_rows)
    blocks = _reply_blocks(parsed_rows)
    prompt = _last_submitted_input(launcher_input_path) or prompt_fallback
    if not blocks:
        return unavailable("capture_unparseable" if prompt else "no_exchange")

    return {
        "available": True,
        "source": "launcher",
        "reason": None,
        "user": (
            {"text": prompt[-_USER_TEXT_CAP:], "timestamp": None}
            if prompt else None
        ),
        "assistant": {
            "text": blocks[-1][-_ASSISTANT_TEXT_CAP:],
            "timestamp": None,
        },
    }


def _read_tail(path: Path, n_bytes: int) -> str:
    return _read_tail_bytes(path, n_bytes).decode("utf-8", errors="replace")


def _nonempty_file(path: Path) -> bool:
    try:
        return Path(path).stat().st_size > 0
    except OSError:
        return False


def _cached_rows(
    path: Path, fingerprint: Optional[Tuple[int, int, int, int]]
) -> Optional[List[Tuple[str, str]]]:
    if fingerprint is None:
        return None
    with _ROWS_CACHE_LOCK:
        hit = _ROWS_CACHE.get(str(path))
    return hit[1] if hit is not None and hit[0] == fingerprint else None


def _store_rows(
    path: Path,
    fingerprint: Optional[Tuple[int, int, int, int]],
    parsed_rows: List[Tuple[str, str]],
) -> None:
    if fingerprint is None:
        return
    with _ROWS_CACHE_LOCK:
        _ROWS_CACHE.pop(str(path), None)
        _ROWS_CACHE[str(path)] = (fingerprint, parsed_rows)
        while len(_ROWS_CACHE) > _ROWS_CACHE_MAX:
            _ROWS_CACHE.pop(next(iter(_ROWS_CACHE)))


def _terminal_rows(raw: str, *, rows: int, cols: int) -> List[Tuple[str, str]]:
    screen = pyte.HistoryScreen(cols, rows, history=_CAPTURE_HISTORY_LINES)
    pyte.Stream(screen).feed(_PRIVATE_DSR.sub("", raw))
    all_rows: Iterable[Any] = list(screen.history.top) + [
        screen.buffer[y] for y in range(screen.lines)
    ]
    return [(_plain_row(row, cols), _row_marker(row, cols)) for row in all_rows]


def _plain_row(row: Any, cols: int) -> str:
    return "".join((row[x].data or " ") for x in range(cols)).rstrip()


def _row_marker(row: Any, cols: int) -> str:
    for x in range(cols):
        cell = row[x]
        char = cell.data or " "
        if not char.strip():
            continue
        if char not in _BULLETS:
            return "none"
        # Claude dims an in-flight tool bullet to neutral grey. Colour spread
        # then looks assistant-like, but the stable ``ToolName(...)`` shape is
        # still definitive and prevents a running Bash/Read block replacing
        # the last completed prose reply in the drawer.
        if _TOOL_CALL_RE.search(_plain_row(row, cols).lstrip()):
            return "tool"
        return "tool" if _is_tool_color(str(cell.fg or "default")) else "assistant"
    return "none"


def _is_tool_color(color: str) -> bool:
    color = color.lower()
    if color in ("default", "white", "brightwhite"):
        return False
    if color in _SATURATED_NAMED:
        return True
    if re.fullmatch(r"[0-9a-f]{6}", color):
        channels = [int(color[i:i + 2], 16) for i in (0, 2, 4)]
        return max(channels) - min(channels) > 60
    return False


def _reply_blocks(rows: List[Tuple[str, str]]) -> List[str]:
    rows = _drop_trailing_composer(rows)
    blocks: List[str] = []
    current: Optional[List[str]] = None
    for text, marker in rows:
        if marker == "assistant":
            _flush_block(current, blocks)
            current = [text]
        elif marker == "tool":
            _flush_block(current, blocks)
            current = None
        elif current is not None:
            current.append(text)
    _flush_block(current, blocks)
    return blocks


def _flush_block(current: Optional[List[str]], blocks: List[str]) -> None:
    if not current:
        return
    prose: List[str] = []
    for line in current:
        stripped = line.strip()
        if (
            _TIMING_RE.search(stripped)
            or _SPINNER_RE.search(stripped)
            or stripped.lower().startswith("recap")
            or _TIP_RE.search(line)
        ):
            break
        prose.append(line)
    text = re.sub(r"\s+", " ", " ".join(prose)).strip()
    text = _LEAD_BULLET_RE.sub("", text)
    if text:
        blocks.append(text)


def _drop_trailing_composer(rows: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    last_rule = -1
    for index in range(len(rows) - 1, -1, -1):
        if _is_rule_line(rows[index][0]):
            last_rule = index
            break
    if last_rule < 0:
        return rows
    top = last_rule
    gap = 0
    for index in range(last_rule - 1, -1, -1):
        if gap > 4:
            break
        if _is_rule_line(rows[index][0]):
            top = index
            gap = 0
        else:
            gap += 1
    return rows[:top]


def _is_rule_line(line: str) -> bool:
    if _RULE_RUN_RE.search(line):
        return True
    nonspace = re.sub(r"\s", "", line)
    if len(nonspace) < 8:
        return False
    return len(_RULE_CHARS_RE.findall(nonspace)) / len(nonspace) >= 0.8


def _last_submitted_input(path: Optional[Path]) -> str:
    """The last line the user actually submitted (CR/LF-terminated) to the
    logged input stream at ``path``.

    Raw input, lines kept raw until a submit so a sequence split across two
    logged chunks still cooks correctly; each completed line runs through
    :func:`_cook_input_line` — the same keystroke cooker
    ``session_host.PtySession._maybe_capture_prompt`` uses for the first-
    prompt title — so the drawer prompt and the session title agree on one
    rule instead of carrying their own, divergent copies.
    """
    if path is None:
        return ""
    lines, truncated = _tail_lines(path, _INPUT_TAIL_BYTES)
    if truncated and lines:
        lines = lines[1:]  # likely torn by the seek
    raw = ""
    submitted: List[str] = []
    for line in lines:
        match = _INPUT_RE.search(line)
        if not match:
            continue
        try:
            chunk = ast.literal_eval(match.group(1))
        except (SyntaxError, ValueError):
            continue
        if not isinstance(chunk, str):
            continue
        raw += chunk
        while True:
            cr = raw.find("\r")
            lf = raw.find("\n")
            ends = [i for i in (cr, lf) if i != -1]
            if not ends:
                break
            end = min(ends)
            text = _cook_input_line(raw[:end]).strip()
            if text:
                submitted.append(text)
            # Skip the terminator, collapsing a paired CR+LF.
            nxt = end + 1
            if raw[end] == "\r" and nxt < len(raw) and raw[nxt] == "\n":
                nxt += 1
            raw = raw[nxt:]
    return submitted[-1] if submitted else ""
