"""The Settings sheet's login-state module refuses a status outside its closed set.

``static/settings/login-state.js`` declares the X login window's state. This shells
out to plain Node to import the real ES module (``tests/js/login_state.test.mjs``).
Machines without Node skip cleanly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "login_state.test.mjs"


def test_ui_reads_no_state_from_a_status_outside_the_set() -> None:
    """Only a ``running`` status is open; anything outside the closed set derives no state.

    state-machine-proof: inspiration-login-state-ui
    """
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "login-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"
    values = json.loads(next(line for line in lines if line.startswith("VALUES "))[len("VALUES "):])
    assert values == ["closed", "failed", "idle", "running"]
