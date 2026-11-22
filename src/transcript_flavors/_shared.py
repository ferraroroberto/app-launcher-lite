"""Generic parsing primitives for the transcript line grammars.

Split out of ``src/session_view/transcript.py`` alongside the per-flavour
modules in this package. Everything here is used by a flavour builder
(``copilot.py``, ``apply_patch.py``) or by the paging code in
``src/session_view/transcript.py`` itself — nothing flavour-specific lives
here, so no flavour module needs to import another flavour module's helpers.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

Line = Tuple[int, str]
Entry = Dict[str, Any]
EntryBuilder = Callable[[List[Line]], List[Entry]]

# Display caps — the terminal stays the escape hatch for anything longer.
ASSISTANT_TEXT_CAP = 12_000
USER_TEXT_CAP = 4_000
THINKING_TEXT_CAP = 2_000
TOOL_SUMMARY_CAP = 200
TOOL_RESULT_CAP = 1_500
SYSTEM_TEXT_CAP = 1_500

# Effectively "no cap" for `entry_full_text`: a turn's raw text never
# approaches this, so `_cap` never reports it truncated.
NO_CAP = 1 << 30


def _cap(text: str, cap: int) -> Tuple[str, bool]:
    text = text or ""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


# Per-thread parse memo, live only inside `parse_memo()`. Pages are built on a
# thread pool, one session per thread, so a shared dict would mix requests.
_PARSE_MEMO = threading.local()
_UNPARSED = object()


@contextlib.contextmanager
def parse_memo() -> Iterator[None]:
    """Parse each raw line at most once for the duration of the block.

    One page read builds the same lines several times (the stop check per
    window, the trim, the live-tail split, the message-key scans), and JSON
    decoding is nearly all of its cost. Safe because no builder mutates a
    parsed line: they copy what they need into fresh entry dicts. Lines are
    keyed by their string, whose hash Python caches on the object, and the
    memo is dropped when the outermost block exits.
    """
    owner = getattr(_PARSE_MEMO, "cache", None) is None
    if owner:
        _PARSE_MEMO.cache = {}
    try:
        yield
    finally:
        if owner:
            _PARSE_MEMO.cache = None


def _loads(raw: str) -> Optional[Dict[str, Any]]:
    cache = getattr(_PARSE_MEMO, "cache", None)
    if cache is not None:
        hit = cache.get(raw, _UNPARSED)
        if hit is not _UNPARSED:
            return hit
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    out = obj if isinstance(obj, dict) else None
    if cache is not None:
        cache[raw] = out
    return out


def _tool_summary(inputs: Any) -> str:
    """One line describing a tool call's input: the first string-valued
    argument (a command, a path, a prompt), else a compact key list."""
    if isinstance(inputs, str):
        text = inputs
    elif isinstance(inputs, dict):
        text = ""
        for value in inputs.values():
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            text = ", ".join(str(k) for k in inputs.keys())
    else:
        text = ""
    text = " ".join(text.split())
    return _cap(text, TOOL_SUMMARY_CAP)[0]


# What a tool call did, in plain words for Chat's runs. The tool names and
# argument keys were probed from real Copilot transcripts (key names only); a
# tool not listed, or an input missing its key, gets no action and renders
# exactly as before.
_COMMAND_TOOLS = {"bash": "command", "powershell": "command"}
_READ_TOOLS = {"view": "path"}
# Each argument is a tuple of key names, the first one present wins. Every key
# was read from a real call or the harness's own tool schema, never guessed.
_WRITE_TOOLS = {"create": (("path",), ("file_text",))}
_EDIT_TOOLS = {"edit": (("path",), ("oldText", "old_str"), ("newText", "new_str"))}
ACTION_COMMAND_CAP = 2_000

# Per-step diffs. An edit/write action carries ``diff``: ``hunks``,
# each ``{"old_start", "new_start", "lines"}`` with every line keeping its
# unified-diff prefix (" ", "-", "+"), plus ``numbered`` and ``truncated``.
# The starts are real file line numbers only where the harness recorded them
# (an ``apply_patch`` Add, or a created file); a diff
# worked out from an edit's own old/new text has none, so they are ``None`` --
# never invented. A builder caps at DIFF_FULL_BYTES (the viewer's cap);
# a transcript page trims each step further, see
# ``src.session_view.transcript._inline_diffs``.
DIFF_FULL_BYTES = 200_000
DIFF_CONTEXT = 3

Hunk = Dict[str, Any]


def _edit_hunks(old: str, new: str) -> Tuple[List[Hunk], int, int]:
    """``(hunks, added, removed)`` between an edit's own old and new text --
    one matcher for both, so the counts are exactly the diff's +/- lines."""
    a, b = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    hunks: List[Hunk] = []
    for group in matcher.get_grouped_opcodes(DIFF_CONTEXT):
        lines: List[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend(" " + line for line in a[i1:i2])
                continue
            if tag in ("replace", "delete"):
                lines.extend("-" + line for line in a[i1:i2])
            if tag in ("replace", "insert"):
                lines.extend("+" + line for line in b[j1:j2])
        hunks.append({"old_start": None, "new_start": None, "lines": lines})
    return hunks, added, removed


def _added_hunks(text: str, *, numbered: bool = False) -> List[Hunk]:
    """A whole new text as one all-added hunk (a Write). ``numbered`` only
    when the file is known to be new, so line 1 really is line 1."""
    lines = ["+" + line for line in text.splitlines()]
    if not lines:
        return []
    return [{
        "old_start": 0 if numbered else None,
        "new_start": 1 if numbered else None,
        "lines": lines,
    }]


def diff_counts(hunks: List[Hunk]) -> Tuple[int, int]:
    """``(added, removed)`` lines of a diff."""
    added = removed = 0
    for h in hunks:
        for line in h["lines"]:
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return added, removed


def cap_diff(hunks: List[Hunk], max_bytes: int, max_lines: Optional[int] = None) -> Dict[str, Any]:
    """``{"hunks", "numbered", "truncated"}`` holding at most ``max_bytes``
    (and ``max_lines``) of diff lines; a hunk cut short keeps what fits."""
    kept: List[Hunk] = []
    used = count = 0
    truncated = False
    for h in hunks:
        lines: List[str] = []
        for line in h["lines"]:
            cost = len(line.encode("utf-8", errors="replace")) + 1
            if used + cost > max_bytes or (max_lines is not None and count >= max_lines):
                truncated = True
                break
            lines.append(line)
            used += cost
            count += 1
        if lines:
            kept.append({**h, "lines": lines})
        if truncated:
            break
    return {
        "hunks": kept,
        "numbered": bool(hunks) and all(h["new_start"] is not None for h in hunks),
        "truncated": truncated,
    }


def _tool_action(name: str, inputs: Any) -> Optional[Dict[str, Any]]:
    """``{"verb": "ran" | "edited" | "wrote" | "read", ...}`` for a known tool, else None.
    (An ``apply_patch`` step can also be ``"deleted"``: built in
    ``apply_patch.py``, not here, since it has no tool-input shape of its own.)
    """
    if not isinstance(inputs, dict):
        return None

    def first(keys: Tuple[str, ...], source: Dict[str, Any]) -> Optional[str]:
        return next((k for k in keys if k in source), None)

    def text(keys: Tuple[str, ...]) -> Optional[str]:
        for key in keys:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    if name in _COMMAND_TOOLS:
        command = text((_COMMAND_TOOLS[name],))
        return {"verb": "ran", "command": _cap(command.strip(), ACTION_COMMAND_CAP)[0]} if command else None
    if name in _READ_TOOLS:
        path = text((_READ_TOOLS[name],))
        return {"verb": "read", "path": path} if path else None
    if name in _WRITE_TOOLS:
        path_keys, body_keys = _WRITE_TOOLS[name]
        path, body_key = text(path_keys), first(body_keys, inputs)
        body = inputs.get(body_key) if body_key else None
        if not path or not isinstance(body, str):
            return None
        return _with_diff(
            {"verb": "wrote", "path": path, "added": len(body.splitlines()), "removed": 0},
            _added_hunks(body),
        )
    if name in _EDIT_TOOLS:
        path_keys, old_keys, new_keys = _EDIT_TOOLS[name]
        path = text(path_keys)
        edits = [inputs] if first(old_keys, inputs) else [e for e in inputs.get("edits") or [] if isinstance(e, dict)]
        pairs = []
        for e in edits:
            old_key, new_key = first(old_keys, e), first(new_keys, e)
            pairs.append((e.get(old_key) if old_key else None, e.get(new_key) if new_key else None))
        pairs = [(o, n) for o, n in pairs if isinstance(o, str) and isinstance(n, str)]
        if not path or not pairs:
            return None
        # A multi-edit (an `edits` list) is one combined
        # diff: each pair's hunks in the order the edits were applied.
        hunks: List[Hunk] = []
        added = removed = 0
        for old, new in pairs:
            h, a, r = _edit_hunks(old, new)
            hunks.extend(h)
            added, removed = added + a, removed + r
        return _with_diff({"verb": "edited", "path": path, "added": added, "removed": removed}, hunks)
    return None


def _with_diff(action: Dict[str, Any], hunks: List[Hunk]) -> Dict[str, Any]:
    """Attach a capped ``diff`` to an edit/write action; an edit that
    changes no line keeps today's action with no diff at all."""
    if hunks:
        action["diff"] = cap_diff(hunks, DIFF_FULL_BYTES)
    return action


def _tool_description(inputs: Any, key: str = "description") -> str:
    """The call's own one-line description, else ``""``. Copilot's
    ``powershell`` carries ``description`` (probed from real transcripts, key
    names only)."""
    value = inputs.get(key) if isinstance(inputs, dict) else None
    if not isinstance(value, str):
        return ""
    return _cap(" ".join(value.split()), TOOL_SUMMARY_CAP)[0]


def _with_action(entry: Entry, action: Optional[Dict[str, Any]], description: str = "") -> Entry:
    """Finish a ``tool_call`` entry: :func:`_tool_action`'s result, when there
    is one, and its ``label`` -- the call's ``description``, else its
    ``summary``. Every flavour's calls pass through here, so a harness that
    records no description still gets a label."""
    if action:
        entry["action"] = action
    entry["label"] = description or entry.get("summary") or ""
    return entry


def _entry(kind: str, offset: int, timestamp: Any, **fields: Any) -> Entry:
    e: Entry = {"kind": kind, "offset": offset, "timestamp": timestamp}
    e.update(fields)
    return e


def _text_entry(kind: str, offset: int, timestamp: Any, text: str, cap: int, **fields: Any) -> Entry:
    body, truncated = _cap(text.strip(), cap)
    return _entry(kind, offset, timestamp, text=body, truncated=truncated, **fields)


def _attach_result(entries: List[Entry], calls: Dict[str, Entry], call_id: Any,
                   text: str, offset: int, timestamp: Any, sidechain: bool,
                   error: bool = False) -> None:
    """Pair one tool result with its call.

    ``error`` is written onto the entry **only when true** — the single
    place every flavour funnels through, so a harness that records a
    failure keeps it this far. Its absence deliberately means "not
    reported as failed", *not* "succeeded": a harness may be unable to
    tell a failed call from a working one at all for some or all of their
    tools, and folding that gap into the success state is the defect this
    fixes rather than a shortcut it may take. Which harness can say what is
    declared once, per flavour, in :data:`src.session_view.transcript.FLAVORS`'
    ``tool_errors`` — the client reads that to decide whether an unmarked
    call means "fine" or "nobody can tell".

    """
    body, truncated = _cap(text.strip(), TOOL_RESULT_CAP)
    call = calls.pop(str(call_id), None) if call_id else None
    if call is not None and call.get("result") is None:
        call["result"] = body
        call["result_truncated"] = truncated
        if error:
            call["error"] = True
            # A failed edit changed nothing: its row keeps today's
            # shape, never a diff of what it only attempted.
            action = call.get("action")
            if isinstance(action, dict):
                action.pop("diff", None)
        return
    # A result whose call fell off this page (or an unknown id): stands alone
    # so it is neither lost nor mis-paired.
    fields: Dict[str, Any] = {"error": True} if error else {}
    entries.append(_entry(
        "tool_result", offset, timestamp, text=body, truncated=truncated,
        tool_use_id=str(call_id or ""), sidechain=sidechain, **fields,
    ))
