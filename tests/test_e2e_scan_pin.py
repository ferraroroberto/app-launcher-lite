"""The e2e webapp's project scan must stay pinned to the checkout (issue #58).

Every page boot fetches ``/api/coding/git-status``, which runs ``git`` once per
scanned project. The disposable webapp inherited the default ``projects_dir``
(the checkout's parent), so on a dev box each browser test spawned git for
every sibling repo and the suite starved itself. ``pin_scan_to_checkout``
leaves only this checkout (and the named real-agent launch target) visible.
"""

from __future__ import annotations

from pathlib import Path

from src.scanner import scan_project_dirs
from tests.e2e.conftest import pin_scan_to_checkout


def _scanned(cfg: dict) -> list:
    return [
        p.name
        for p in scan_project_dirs(Path(cfg["projects_dir"]), cfg["projects_ignore"])
    ]


def test_only_checkout_and_kept_siblings_are_scanned(tmp_path: Path) -> None:
    for name in ("app", "tool", "other", "weird[1]"):
        (tmp_path / name).mkdir()
    cfg = pin_scan_to_checkout({}, tmp_path / "app", keep=("tool",))
    assert cfg["projects_dir"] == str(tmp_path)
    assert _scanned(cfg) == ["app", "tool"]


def test_bracketed_sibling_name_matches_only_itself(tmp_path: Path) -> None:
    for name in ("app", "weird[1]", "weird1"):
        (tmp_path / name).mkdir()
    assert _scanned(pin_scan_to_checkout({}, tmp_path / "app")) == ["app"]


def test_inherited_ignore_pattern_cannot_hide_the_checkout(tmp_path: Path) -> None:
    for name in ("app-wt-1", "app-old"):
        (tmp_path / name).mkdir()
    cfg = pin_scan_to_checkout(
        {"projects_ignore": ["*-wt-*", "keepme"]}, tmp_path / "app-wt-1"
    )
    assert "*-wt-*" not in cfg["projects_ignore"]
    assert "keepme" in cfg["projects_ignore"]
    assert _scanned(cfg) == ["app-wt-1"]
