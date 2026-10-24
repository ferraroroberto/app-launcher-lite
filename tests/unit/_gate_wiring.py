"""Shared checks for the per-stage ratchet-gate wiring tests.

Every ratchet stage in ``scripts/verify-before-ship.ps1`` is a ``python -m
scripts.<stage>_gate`` call whose exit code must fail closed, so the verdict check
and the stage-order read are written once here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]


def gate_stages() -> list[str]:
    """The ``scripts.*`` modules the pre-ship gate invokes, in run order."""
    lines = (ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8").splitlines()
    return [ln.split("-m ")[-1] for ln in lines if ln.lstrip().startswith("&") and "-m scripts." in ln]


def assert_fails_closed(classify_exit: Callable[[int], tuple[bool, str]]) -> None:
    """Exit 0 is the only pass; exit 3 reads as NOT CONFIRMED, never a skip."""
    verdicts = {code: classify_exit(code) for code in range(-1, 6)}
    assert [code for code, (ok, _) in verdicts.items() if ok] == [0]
    assert all(label for _, label in verdicts.values())
    assert "NOT CONFIRMED" in verdicts[3][1] and "skip" not in verdicts[3][1].lower()
