"""The Copilot side under ``copilot/``: its own unittest suite.

``copilot/`` holds Lite's hooks, chief helpers and lane skills. Its tests are
stdlib ``unittest`` modules that set up their own ``sys.path`` from their
location, so they run in a child interpreter rooted at ``copilot/`` rather
than inside this pytest process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_COPILOT = _REPO_ROOT / "copilot"
# The suite runs in about 40 s on this host; the cap leaves room for a
# loaded box without letting a hung child hold the gate.
_SUITE_TIMEOUT_S = 300


@pytest.mark.windows_only
def test_the_copilot_suite_passes() -> None:
    # -B and PYTHONDONTWRITEBYTECODE keep __pycache__ out of the copilot/ code folders.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
        cwd=_COPILOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_SUITE_TIMEOUT_S,
        creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-8000:]


pytestmark = pytest.mark.timeout(_SUITE_TIMEOUT_S + 30)
