"""Unit pin for the dictation PCM capture's resampler (``shared/voice-pcm.js``).

A 48 kHz or 44.1 kHz microphone must become 16 kHz samples of the right count
and level. Pinned without a browser by shelling out to plain Node
(``tests/js/voice_pcm_resampler.test.mjs``). Machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "voice_pcm_resampler.test.mjs"


def test_pcm_resampler() -> None:
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        f"PCM resampler failed its Node-side pin:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
