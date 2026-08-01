"""Agent-aware conversation previews for the Board drawer (issue #457).

The hook row's native JSONL remains the best source when it exists because it
is structured chat data.  Launcher-owned PTYs also have an exact-id capture,
however, and that is the common fallback for sessions whose declared JSONL is
absent and for agents that publish no hook transcript at all.

The capture is terminal output, not prose.  A bounded tail is replayed through
``pyte`` and reply blocks are selected by the same leading-bullet colour
contract as the browser's read-aloud extractor.  This keeps ANSI/full-screen
repaint bytes out of the API response and avoids any cwd-based guessing.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pyte

from src.board_transcript import last_exchange

_CAPTURE_TAIL_BYTES = 512 * 1024
_CAPTURE_HISTORY_LINES = 2500
_ASSISTANT_TEXT_CAP = 6000
_USER_TEXT_CAP = 1500

_BULLETS = frozenset({"●", "⏺", "•", "◉", "○"})
_SATURATED_NAMED = frozenset({
    "black", "red", "green", "brown", "blue", "magenta", "cyan",
    "brightblack", "brightred", "brightgreen", "brightblue",
    "brightmagenta", "brightcyan",
})
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
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def unavailable(reason: str) -> Dict[str, Any]:
    """Canonical unavailable response with a machine-readable reason."""
    return {
        "available": False,
        "source": None,
        "reason": reason,
        "user": None,
        "assistant": None,
    }


def resolve_exchange(
    session: Dict[str, Any],
    native_path: Any,
    launcher_capture_path: Path,
    launcher_input_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve one live session's exchange through the source hierarchy."""
    native = last_exchange(native_path)
    if native.get("available"):
        return {**native, "source": "native", "reason": None}

    fallback = launcher_last_exchange(
        launcher_capture_path,
        launcher_input_path=launcher_input_path,
        prompt_fallback=str(session.get("prompt_title") or "").strip(),
        rows=int(session.get("rows") or 42),
        cols=int(session.get("cols") or 120),
    )
    if fallback.get("available"):
        return fallback

    native_declared = bool(native_path)
    capture_exists = _nonempty_file(launcher_capture_path)
    if capture_exists:
        reason = str(fallback.get("reason") or "capture_unparseable")
    elif native_declared:
        reason = "native_unavailable"
    else:
        reason = "no_exchange"
    return unavailable(reason)


def launcher_last_exchange(
    capture_path: Path,
    *,
    launcher_input_path: Optional[Path] = None,
    prompt_fallback: str = "",
    rows: int = 42,
    cols: int = 120,
) -> Dict[str, Any]:
    """Extract the latest reply from an exact-id PTY capture tail."""
    raw = _read_tail(capture_path, _CAPTURE_TAIL_BYTES)
    if not raw:
        return unavailable("no_exchange")

    parsed_rows = _terminal_rows(raw, rows=max(2, rows), cols=max(20, cols))
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
    try:
        with Path(path).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _nonempty_file(path: Path) -> bool:
    try:
        return Path(path).stat().st_size > 0
    except OSError:
        return False


def _terminal_rows(raw: str, *, rows: int, cols: int) -> List[Tuple[str, str]]:
    screen = pyte.HistoryScreen(cols, rows, history=_CAPTURE_HISTORY_LINES)
    pyte.Stream(screen).feed(raw)
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
    if path is None:
        return ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    buffer = ""
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
        chunk = chunk.replace("\x1b[200~", "").replace("\x1b[201~", "")
        chunk = _CSI_RE.sub("", chunk)
        for char in chunk:
            if char in ("\x7f", "\b"):
                buffer = buffer[:-1]
            elif char in ("\r", "\n"):
                text = buffer.strip()
                if text:
                    submitted.append(text)
                buffer = ""
            elif char == "\t" or ord(char) >= 32:
                buffer += char
    return submitted[-1] if submitted else ""
