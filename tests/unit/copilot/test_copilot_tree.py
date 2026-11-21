"""The copied Copilot side under ``copilot/``: provenance and its own suite.

``copilot/`` holds the hooks, chief helpers and lane skills copied from
fleet-config-lite. ``copilot/provenance.json`` names the source commit and
either a hash (byte-identical) or a reason (changed on purpose) for every file,
so a silent edit or a stray file shows up here instead of as an unexplained
fork. The copied tests are stdlib ``unittest`` modules that set up their own
``sys.path`` from their location, so they run in a child interpreter exactly
as they did in the source repo rather than inside this pytest process.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_COPILOT = _REPO_ROOT / "copilot"
_MANIFEST = _COPILOT / "provenance.json"
# The copied suite runs in about 40 s on this host; the cap leaves room for a
# loaded box without letting a hung child hold the gate.
_SUITE_TIMEOUT_S = 300


def _digest(path: Path) -> str:
    """sha256 with CRLF read as LF, so a checkout's line-ending conversion is not drift."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _tree_files() -> set[str]:
    return {
        p.relative_to(_COPILOT).as_posix()
        for p in _COPILOT.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    } - {_MANIFEST.name}


def test_every_copied_file_matches_its_source_or_says_why_not() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    identical: dict[str, str] = manifest["files"]
    modified: dict[str, str] = manifest["modified"]
    added: dict[str, str] = manifest["added"]
    assert not set(identical) & set(modified), "a file is listed as both identical and modified"
    assert not (set(identical) | set(modified)) & set(added), "a copied file is also listed as added"
    assert all(reason.strip() for reason in (*modified.values(), *added.values())), "a modified or added file has no reason"

    present = _tree_files()
    listed = set(identical) | set(modified) | set(added)
    assert not present - listed, f"files under copilot/ with no provenance entry: {sorted(present - listed)}"
    assert not listed - present, f"provenance entries with no file: {sorted(listed - present)}"
    drifted = sorted(rel for rel, want in identical.items() if _digest(_COPILOT / rel) != want)
    assert not drifted, (
        f"copilot/ files no longer byte-identical to fleet-config-lite@{manifest['commit'][:7]}: {drifted}; "
        "move each to 'modified' with its reason, or re-copy it from the source"
    )


@pytest.mark.windows_only
def test_the_copied_suite_passes() -> None:
    # -B and PYTHONDONTWRITEBYTECODE keep __pycache__ out of the copied code folders.
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
