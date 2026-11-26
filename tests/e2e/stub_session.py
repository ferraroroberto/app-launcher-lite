"""The lightweight PTY stub the e2e gate launches instead of the real Copilot CLI.

Kept out of the e2e conftest so a plain script can launch the same
deterministic child the gate does. No pytest import.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Sentinel flag for the lightweight PTY child (issue #534). Under autoboot the
# disposable session-host's PATH is prepended with a harness-generated
# `copilot.cmd` shim: a launch whose flags are exactly this sentinel runs a
# tiny deterministic Python echo loop instead of the real Copilot CLI, while
# any other flag set falls through to the real `copilot`.
# Purely a harness substitution — no production code knows about it.
STUB_FLAG = "--e2e-stub"
# The banner that child prints on startup — the marker that identifies a
# transcript as harness-written (issue #913's isolation check).
STUB_BANNER = "[e2e-stub]"
# The stub child's file name, which is how the gate's teardown recognises a
# leaked child in a process's argv (issue #1335).
STUB_SCRIPT_NAME = "e2e_stub_child.py"
# The one coding agent this fork launches, and so the command the shim stands
# in for (src/agents.py: Copilot's `copilot`, quit with `/exit`).
STUB_AGENT = "copilot"


STUB_CHILD_SOURCE = '''\
"""Deterministic lightweight PTY child for UI-only e2e tests (issue #534).

Stands in for the real Copilot CLI under the disposable autoboot session-host:
instant startup, echoes each input line (ConPTY cooked mode echoes keystrokes
too), exits on /exit (or /quit) so the host's graceful stop path works.
/fullscreen turns it into a full-screen TUI: it repaints a frame laid out for
the terminal's size, on every size change and only then, as Copilot does.
"""
import os
import sys
import threading
import time


def paint_on_resize():
    drawn = None
    while True:
        size = os.get_terminal_size()
        if size != drawn:
            drawn = size
            cols, rows = size
            frame = "".join(
                "\\x1b[%d;1H%s" % (row, ("row %02d of %dx%d " % (row, rows, cols)).ljust(cols - 1, "."))
                for row in range(1, rows + 1)
            )
            sys.stdout.write("\\x1b[H\\x1b[2J" + frame)
            sys.stdout.flush()
        time.sleep(0.1)


print("[e2e-stub] lightweight PTY child ready (issue #534)", flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    text = line.rstrip("\\r\\n")
    if text.strip() in ("/exit", "/quit"):
        print("[e2e-stub] bye", flush=True)
        break
    if text.strip() == "/fullscreen":
        threading.Thread(target=paint_on_resize, daemon=True).start()
        continue
    print(text, flush=True)
'''

# Drift guard: the e2e fixtures' `leaked_stub_sessions` identifies a harness-written transcript
# by this banner, so the child it comes from must actually print it.
assert STUB_BANNER in STUB_CHILD_SOURCE


def write_claude_shim(shim_dir: Path) -> None:
    """Generate the `copilot.cmd` PATH shim + stub child script (issue #534).

    The session-host spawns agents via ``cmd /c … && copilot <flags>`` with
    the command resolved off its own PATH, so prepending this directory to the
    *disposable* session-host's PATH intercepts every copilot launch: the
    ``--e2e-stub`` sentinel routes to the stub child, anything else falls
    through to the real ``copilot`` resolved at generation time. Where copilot
    isn't installed (the CI runner) the fall-through branch fails loud — but
    it is never reached there, because `launched_claude_pty_session` skips
    first (same `shutil.which` guard as always).
    """
    stub_py = shim_dir / STUB_SCRIPT_NAME
    stub_py.write_text(STUB_CHILD_SOURCE, encoding="utf-8")
    real_agent = shutil.which(STUB_AGENT)
    if real_agent:
        real_branch = f'call "{real_agent}" %*\nexit /b %ERRORLEVEL%\n'
    else:
        real_branch = (
            f"echo [e2e-shim] real {STUB_AGENT} is not installed 1>&2\n"
            "exit /b 1\n"
        )
    shim = (
        "@echo off\n"
        f'if "%~1"=="{STUB_FLAG}" (\n'
        f'  "{sys.executable}" -X utf8 "{stub_py}"\n'
        "  exit /b %ERRORLEVEL%\n"
        ")\n"
        f"{real_branch}"
    )
    # Text-mode write translates \n -> os.linesep, so the .cmd lands with
    # proper CRLF line endings on Windows.
    (shim_dir / f"{STUB_AGENT}.cmd").write_text(shim, encoding="ascii")

