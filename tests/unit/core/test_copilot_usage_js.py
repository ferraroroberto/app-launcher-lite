"""The Usage card's reading logic (``copilot-usage.js``) under plain Node.

Runs ``tests/js/copilot_usage.test.mjs`` (same pattern as
``test_duration_days``). Machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def test_copilot_usage_js() -> None:
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "copilot_usage.test.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "OK" in result.stdout
