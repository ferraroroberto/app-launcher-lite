"""Lite's Copilot state folder: every tier resolves the same one, and the move loses no write.

The hook tier (``copilot/hooks/_lib.py``), the skill tier (``fleet_core/hooks_state.py``), the
webapp (``src/chief/copilot_state.py``) and the installer each spell the folder themselves (the tree
boundary keeps them from importing each other), so this pins them equal, before and after a
migration. The sweep cases replay a chief writing during ``install.py --migrate``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from src.chief import copilot_state

_COPILOT = Path(__file__).resolve().parents[3] / "copilot"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(copilot_state.STATE_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    return tmp_path


def _resolvers() -> dict:
    hooks_lib = _load("lite_hooks_lib", _COPILOT / "hooks" / "_lib.py")
    sys.path.insert(0, str(_COPILOT / "skills" / "_lib"))
    try:
        hooks_state = _load("lite_hooks_state", _COPILOT / "skills" / "_lib" / "fleet_core" / "hooks_state.py")
    finally:
        sys.path.remove(str(_COPILOT / "skills" / "_lib"))
    install_state = _load("lite_install_state", _COPILOT / "install_state.py")
    assert hooks_lib.STATE_DIRNAME == hooks_state.STATE_DIRNAME == install_state.STATE_DIRNAME == copilot_state.STATE_DIRNAME
    return {"hooks": hooks_lib.state_dir, "skills": hooks_state.state_dir, "webapp": copilot_state.state_dir}


def test_every_tier_resolves_the_same_folder_before_and_after_a_migration(home: Path) -> None:
    resolvers = _resolvers()
    current = home / ".copilot" / copilot_state.STATE_DIRNAME
    legacy = home / ".copilot" / "hooks" / "state"
    assert {name: f() for name, f in resolvers.items()} == dict.fromkeys(resolvers, current)
    legacy.mkdir(parents=True)
    assert {name: f() for name, f in resolvers.items()} == dict.fromkeys(resolvers, legacy)
    current.mkdir()
    assert {name: f() for name, f in resolvers.items()} == dict.fromkeys(resolvers, current)


def test_copilot_home_moves_every_tier_and_the_installer_to_the_same_folder(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("COPILOT_HOME", str(elsewhere))
    monkeypatch.syspath_prepend(str(_COPILOT))
    install = _load("lite_install", _COPILOT / "install.py")
    expected = elsewhere / copilot_state.STATE_DIRNAME
    assert Path(install.Paths().state) == expected
    resolvers = _resolvers()
    assert {name: f() for name, f in resolvers.items()} == dict.fromkeys(resolvers, expected)
    legacy = elsewhere / "hooks" / "state"
    legacy.mkdir(parents=True)
    assert {name: f() for name, f in resolvers.items()} == dict.fromkeys(resolvers, legacy)
    monkeypatch.setenv(copilot_state.STATE_DIR_ENV_VAR, str(tmp_path / "override"))
    assert {name: f() for name, f in resolvers.items()} == dict.fromkeys(resolvers, tmp_path / "override")


def test_a_setting_pinned_at_the_old_default_reads_through_to_the_state_folder(home: Path, tmp_path: Path) -> None:
    from src import webapp_config

    legacy = home / ".copilot" / "hooks" / "state"
    config = tmp_path / "webapp_config.json"
    config.write_text(
        '{"chief_inbox_dir": "%s", "chief_plan_file": "%s"}' % (
            str(legacy / "chief-inbox").replace("\\", "\\\\"), str(tmp_path / "own.json").replace("\\", "\\\\")),
        encoding="utf-8")
    (home / ".copilot" / copilot_state.STATE_DIRNAME).mkdir(parents=True)
    cfg = webapp_config.load_webapp_config(config)
    assert Path(cfg.chief_inbox_dir) == home / ".copilot" / copilot_state.STATE_DIRNAME / "chief-inbox"
    assert Path(cfg.chief_plan_file) == tmp_path / "own.json"


def test_writes_made_during_the_move_all_land_in_the_new_folder(tmp_path: Path) -> None:
    install_state = _load("lite_install_state", _COPILOT / "install_state.py")
    legacy, current = tmp_path / "hooks" / "state", tmp_path / "app-launcher-lite-state"
    (legacy / "chief-inbox").mkdir(parents=True)
    (legacy / "chief-plan.json").write_text("plan v1", encoding="utf-8")
    (legacy / "chief-managed.json").write_text("registry v1", encoding="utf-8")
    (legacy / "chief-inbox" / "acked.json").write_text("{}", encoding="utf-8")
    (legacy / "chief-inbox" / "unchanged.json").write_text("{}", encoding="utf-8")

    snapshot = install_state.copy_tree(str(legacy), str(current) + ".migrating")
    (current.parent / (current.name + ".migrating")).rename(current)
    # Before the switch reached it, a writer still on the old folder: a new event and a newer plan.
    (legacy / "chief-inbox" / "late-event.json").write_text('{"kind": "turn-end"}', encoding="utf-8")
    (legacy / "chief-plan.json").write_text("plan v2, longer", encoding="utf-8")
    (legacy / "chief-inbox" / ".late.json.abc.tmp").write_text("half", encoding="utf-8")
    # After the switch: the chief acked one event and rewrote the registry in the new folder.
    (current / "chief-inbox" / "acked.json").unlink()
    (current / "chief-managed.json").write_text("registry v2", encoding="utf-8")

    install_state.sweep(str(legacy), str(current), snapshot, final=False)
    assert (legacy / "chief-inbox" / ".late.json.abc.tmp").exists(), "a write in flight is left to its writer"
    lines = install_state.sweep(str(legacy), str(current), snapshot, final=True)
    assert install_state.remove_empty_dirs(str(legacy))

    assert (current / "chief-inbox" / "late-event.json").read_text(encoding="utf-8") == '{"kind": "turn-end"}'
    assert (current / "chief-plan.json").read_text(encoding="utf-8") == "plan v2, longer"
    assert (current / "chief-managed.json").read_text(encoding="utf-8") == "registry v2"
    assert not (current / "chief-inbox" / "acked.json").exists(), "an event acked after the copy stays acked"
    assert (current / "chief-inbox" / "unchanged.json").exists()
    assert any("moved across" in line for line in lines)


def test_a_file_changed_in_both_folders_keeps_the_new_one_and_sets_the_old_beside_it(tmp_path: Path) -> None:
    install_state = _load("lite_install_state", _COPILOT / "install_state.py")
    legacy, current = tmp_path / "old", tmp_path / "new"
    legacy.mkdir()
    current.mkdir()
    (legacy / "sessions-state.json").write_text("old side", encoding="utf-8")
    (current / "sessions-state.json").write_text("new side", encoding="utf-8")
    lines = install_state.sweep(str(legacy), str(current), {}, final=True)
    assert (current / "sessions-state.json").read_text(encoding="utf-8") == "new side"
    assert (current / ("sessions-state.json" + install_state.ASIDE_SUFFIX)).read_text(encoding="utf-8") == "old side"
    assert any(line.startswith("[warn] changed in both folders") for line in lines)
