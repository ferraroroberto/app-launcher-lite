"""After a migration, cards in a ``<app>-new`` / ``<app>-e2e`` sibling move to the app's own folder.

Only those: every other card is written back as it was, and a card is never repointed onto a file
another card already starts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from src.launching import repoint_cards
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[3]


def _folder(root: Path, name: str, *files: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for file in files:
        (folder / file).write_text("@echo off\n", encoding="ascii")
    return folder


def _registry(app: Path, bats: List[Path]) -> Path:
    path = app / "config" / "apps.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"id": f"card-{i}", "name": f"Card {i}", "kind": "tray", "bat_path": str(bat),
             "autostart": True, "extra": i} for i, bat in enumerate(bats)]
    path.write_text(json.dumps({"scan_root": str(app.parent), "apps": rows}), encoding="utf-8")
    return path


def _bats(registry: Path) -> List[str]:
    return [row["bat_path"] for row in json.loads(registry.read_text(encoding="utf-8"))["apps"]]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name in ("app-launcher-lite", "app-launcher-lite-new", "app-launcher-lite-e2e",
                 "app-launcher-lite-old", "other-new"):
        _folder(tmp_path, name, "tray.bat", "webapp.bat")
    return tmp_path


def test_only_suffixed_sibling_cards_move_to_the_app_folder(root: Path) -> None:
    app = root / "app-launcher-lite"
    untouched = [root / "app-launcher-lite-old" / "tray.bat", root / "other-new" / "tray.bat",
                 root / "elsewhere" / "app-launcher-lite-new" / "tray.bat"]
    registry = _registry(app, [root / "app-launcher-lite-new" / "tray.bat",
                               root / "app-launcher-lite-e2e" / "webapp.bat", *untouched])
    before = json.loads(registry.read_text(encoding="utf-8"))

    outcomes = repoint_cards.repoint(registry, app)

    assert _bats(registry) == [str(app / "tray.bat"), str(app / "webapp.bat"), *map(str, untouched)]
    after = json.loads(registry.read_text(encoding="utf-8"))
    for row in after["apps"] + before["apps"]:
        row.pop("bat_path")
    assert after == before, "nothing but bat_path changed"
    assert [(o.state, o.name) for o in outcomes] == [("repointed", "Card 0"), ("repointed", "Card 1")]


def test_a_card_is_never_repointed_onto_a_file_another_card_starts(root: Path) -> None:
    app = root / "app-launcher-lite"
    bats = [app / "tray.bat", root / "app-launcher-lite-new" / "tray.bat",
            root / "app-launcher-lite-new" / "webapp.bat", root / "app-launcher-lite-e2e" / "webapp.bat",
            root / "app-launcher-lite-e2e" / "gone.bat"]
    registry = _registry(app, bats)

    outcomes = repoint_cards.repoint(registry, app)

    assert _bats(registry) == [str(app / "tray.bat"), str(bats[1]), str(app / "webapp.bat"),
                               str(bats[3]), str(bats[4])]
    assert [o.state for o in outcomes] == ["left", "repointed", "left", "left"]


def test_what_if_reads_the_old_copy_and_writes_nothing(root: Path, caplog) -> None:
    app, old = root / "app-launcher-lite", root / "app-launcher-lite-old"
    card = root / "app-launcher-lite-new" / "tray.bat"
    registry = _registry(old, [card])
    caplog.set_level("INFO")

    code = repoint_cards.main(["-MigrateFrom", str(old), "-WhatIf", "-StopOld"], app_dir=app)

    assert code == 0 and _bats(registry) == [str(card)]
    assert not (app / "config" / "apps.json").exists()
    assert "would repoint" in caplog.text and str(app / "tray.bat") in caplog.text


def test_an_unreadable_registry_is_reported_and_left_alone(root: Path, caplog) -> None:
    app = root / "app-launcher-lite"
    registry = _registry(app, [])
    registry.write_text("{not json", encoding="utf-8")

    assert repoint_cards.main(["-MigrateFrom", str(root / "x")], app_dir=app) == 1
    assert registry.read_text(encoding="utf-8") == "{not json"
    assert "not checked" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="setup.bat is Windows-only")
@pytest.mark.windows_only
def test_setup_bat_repoints_after_a_migration(root: Path) -> None:
    """The real setup.bat beside a stand-in installer: a migration ends on the repoint."""
    app = root / "app-launcher-lite"
    shutil.copyfile(REPO_ROOT / "setup.bat", app / "setup.bat")
    shutil.copytree(REPO_ROOT / "src", app / "src",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (app / "src" / "installer.py").write_text("raise SystemExit(0)\n", encoding="ascii")
    registry = _registry(app, [root / "app-launcher-lite-new" / "tray.bat"])
    env = {**os.environ, "PYTHONUTF8": "1"}
    env.pop("PYTHONPATH", None)

    done = subprocess.run(
        ["cmd.exe", "/d", "/c", str(app / "setup.bat"), "-MigrateFrom", str(root / "app-launcher-lite-old")],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120, creationflags=NO_WINDOW,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert _bats(registry) == [str(app / "tray.bat")]
    assert "repointed" in done.stderr
