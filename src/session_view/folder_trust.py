"""Copilot's "Confirm folder trust" dialog, read off the live terminal screen.

Copilot asks this once per folder it has not trusted (``trustedFolders`` in
``config.json`` under the Copilot home), before it reads its first message.
``--allow-all`` does not answer it. While it is up the session already has its
window title and has stopped repainting, so a readiness check on the title
alone calls the session ready and the first message is typed into the dialog
and lost; a launch prompt (``-i``) waits behind the dialog until it is
answered. The screen is the only place the dialog is visible, so the server
renders the session's PTY capture (:mod:`src.session_view.capture_screen`) and
reads the dialog off it.

What the dialog looks like (probed on Copilot CLI 1.0.114, 120 columns)::

    ╭──────────────────────────────────────╮
    │ Confirm folder trust                 │
    │ ──────────────────────────────────── │
    │ ╭──────────────────────────────────╮ │
    │ │ <projects>\\some-project          │ │
    │ ╰──────────────────────────────────╯ │
    │ ...                                  │
    │ Do you trust the files in this folder? │
    │ ❯ 1. Yes                             │
    │   2. Yes, and remember this folder for future sessions │
    │   3. No (Esc)                        │
    ╰──────────────────────────────────────╯

A digit alone picks its option, with no Enter. Option 2 makes Copilot add the
folder to ``trustedFolders`` itself, so trust is recorded by Copilot's own
mechanism and never by editing its state file. Option 1 trusts the folder for
that session only, so the question returns next launch.

Anything this module cannot recognise reads as "not showing", and then nothing
is refused and nothing is typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import audit
from src.session_view import capture_screen

TITLE = "Confirm folder trust"
# The option that makes Copilot record the folder, matched on its own label so
# a reworded or reordered menu is never answered blind.
REMEMBER_LABEL = "remember this folder"

# Box-drawing and cursor glyphs around the text: the dialog's frame and ❯.
_FRAME = "│╭╮╰╯─┃ \u276f"
_OPTION_RE = re.compile(r"^(\d)\.\s+(\S.*)$")


@dataclass(frozen=True)
class FolderTrustPrompt:
    """The dialog as the screen shows it: the folder it names and the digit
    of its "remember" option."""

    folder: str
    remember_key: str


def _inner(line: str) -> str:
    """One screen row with the dialog's frame stripped from both ends."""
    return line.strip(_FRAME).strip()


def parse_prompt(lines: List[str]) -> Optional[FolderTrustPrompt]:
    """The folder-trust dialog on this screen, or ``None`` when it isn't
    showing or can't be answered (no folder shown, no "remember" option)."""
    inner = [_inner(line) for line in lines]
    try:
        start = max(i for i, text in enumerate(inner) if text == TITLE)
    except ValueError:
        return None
    folder = ""
    remember = ""
    for text in inner[start + 1:]:
        if not text:
            continue
        match = _OPTION_RE.match(text)
        if match is not None:
            if REMEMBER_LABEL in match.group(2).lower():
                remember = match.group(1)
            continue
        if not folder and not set(text) <= set(_FRAME):
            folder = text
    if not folder or not remember:
        return None
    return FolderTrustPrompt(folder=folder, remember_key=remember)


def read_prompt(path: Path, rows: int, cols: int) -> Optional[FolderTrustPrompt]:
    """The dialog on the capture at ``path`` rendered at the PTY's size, or
    ``None`` when it is not showing or the capture cannot be read."""
    lines = capture_screen.read_screen(path, rows, cols)
    return None if lines is None else parse_prompt(lines)


def pending_for_session(sess: Dict[str, Any]) -> Optional[FolderTrustPrompt]:
    """The dialog a live launcher Copilot session is sitting on, or ``None``.

    Only a session that has not yet taken a prompt is read: the dialog comes
    before the first message, so once the session has a prompt title the
    dialog was answered and an active session's constantly changing capture
    is never re-rendered for it. A detached session has no screen here.
    """
    sid = str(sess.get("session_id") or "")
    if (
        not sid
        or sess.get("kind", "pty") != "pty"
        or str(sess.get("agent") or "").lower() != "copilot"
        or not sess.get("alive", True)
        or sess.get("prompt_title")
    ):
        return None
    rows = int(sess.get("rows") or 40)
    cols = int(sess.get("cols") or 120)
    return read_prompt(audit.transcript_path(sid), rows, cols)


def answer_keys(prompt: FolderTrustPrompt) -> List[Tuple[str, bool]]:
    """The keystrokes that answer "Yes, and remember this folder": its digit,
    no Enter."""
    return [(prompt.remember_key, False)]


def folder_name(folder: str) -> str:
    """The folder's last path part, for a message that names it without the
    whole path."""
    return Path(folder.replace("\\", "/")).name or folder
