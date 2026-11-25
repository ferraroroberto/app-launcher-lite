"""``src.registry.load_registry`` strict-mode read-failure signaling (#925).

A missing ``config/apps.json`` legitimately means "empty registry" for every
caller. A file that exists but fails to parse/read is different — most
callers (Apps-tab scan/save, the CLI scan command) still tolerate that as an
empty registry unchanged, but ``app.tray.registered_trays.launch_all()``
needs to tell it apart from "nothing configured" so it can retry a transient
boot-time glitch instead of silently no-op'ing. ``strict=True`` is the knob
that lets it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.registry import RegistryReadError, load_registry


def test_missing_file_returns_empty_registry(tmp_path: Path):
    registry = load_registry(tmp_path / "does-not-exist.json")
    assert registry.apps == []


def test_missing_file_returns_empty_registry_even_when_strict(tmp_path: Path):
    """A missing file is never treated as a read failure — strict mode only
    changes behavior for a file that exists but can't be parsed."""
    registry = load_registry(tmp_path / "does-not-exist.json", strict=True)
    assert registry.apps == []


def test_corrupt_file_returns_empty_registry_by_default(tmp_path: Path):
    path = tmp_path / "apps.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    registry = load_registry(path)
    assert registry.apps == []


def test_corrupt_file_raises_in_strict_mode(tmp_path: Path):
    path = tmp_path / "apps.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    with pytest.raises(RegistryReadError):
        load_registry(path, strict=True)


# --------------------------------------------------- unique app ids

import json
import logging

from src.registry import (
    AmbiguousAppId,
    AppEntry,
    Registry,
    discover_new,
    get_by_id,
    persist_additions,
    remove_by_id,
    rename_by_id,
    set_autostart_by_id,
)


def _row(app_id: str, bat: str, name: str = "App") -> dict:
    return {"id": app_id, "name": name, "kind": "tray", "bat_path": bat}


def _write_rows(path: Path, rows: list) -> None:
    path.write_text(json.dumps({"scan_root": "", "apps": rows}), encoding="utf-8")


def test_discover_new_gives_a_colliding_candidate_a_unique_id(tmp_path: Path):
    """A bat outside ``scan_root`` is id'd from its file name alone, so a
    second ``tray.bat`` elsewhere would otherwise get the id already taken."""
    scan_root = tmp_path / "root"
    (scan_root / "a").mkdir(parents=True)
    (scan_root / "b").mkdir(parents=True)
    for folder in ("a", "b"):
        (scan_root / folder / "tray.bat").write_text("@echo off", encoding="utf-8")
    existing = Registry(
        scan_root=str(scan_root),
        apps=[AppEntry(id="a-tray", name="A", kind="tray", bat_path="elsewhere.bat")],
    )
    new = discover_new(scan_root=scan_root, existing=existing)
    ids = [e.id for e in new]
    assert len(ids) == len(set(ids))
    assert "a-tray" not in ids


def test_persist_additions_suffixes_a_colliding_id_instead_of_dropping_it(
    tmp_path: Path, monkeypatch
):
    from src import registry as registry_mod

    monkeypatch.setattr(registry_mod, "DEFAULT_REGISTRY_PATH", tmp_path / "apps.json")
    reg = Registry(
        scan_root="",
        apps=[AppEntry(id="tray", name="Hub", kind="tray", bat_path="C:/hub/tray.bat")],
    )
    second = AppEntry(id="tray", name="Stale", kind="tray", bat_path="C:/old/tray.bat")
    added = persist_additions(reg, [second], tmp_path)
    assert [a.id for a in added] == ["tray-2"]
    assert sorted(a.id for a in reg.apps) == ["tray", "tray-2"]


def test_persist_additions_still_skips_a_bat_already_registered(
    tmp_path: Path, monkeypatch
):
    from src import registry as registry_mod

    monkeypatch.setattr(registry_mod, "DEFAULT_REGISTRY_PATH", tmp_path / "apps.json")
    reg = Registry(
        scan_root="",
        apps=[AppEntry(id="tray", name="Hub", kind="tray", bat_path="C:/hub/tray.bat")],
    )
    again = AppEntry(id="tray", name="Hub", kind="tray", bat_path="C:/hub/tray.bat")
    assert persist_additions(reg, [again], tmp_path) == []


def test_load_migrates_duplicate_ids_once_with_a_warning_per_duplicate(
    tmp_path: Path, caplog
):
    path = tmp_path / "apps.json"
    _write_rows(
        path,
        [
            _row("tray", "C:/hub/tray.bat", "Hub"),
            _row("tray", "C:/old/tray.bat", "Stale"),
            _row("tray", "C:/older/tray.bat", "Older"),
            _row("other", "C:/o/other.bat"),
        ],
    )
    with caplog.at_level(logging.WARNING, logger="src.registry"):
        reg = load_registry(path)
    assert [a.id for a in reg.apps] == ["tray", "tray-2", "tray-3", "other"]
    assert [a.name for a in reg.apps][:3] == ["Hub", "Stale", "Older"]
    dup_warnings = [r for r in caplog.records if "duplicate app id" in r.getMessage().lower()]
    assert len(dup_warnings) == 2

    # Persisted: a second load is clean and warns about nothing.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.registry"):
        again = load_registry(path)
    assert [a.id for a in again.apps] == ["tray", "tray-2", "tray-3", "other"]
    assert not [r for r in caplog.records if "duplicate" in r.getMessage().lower()]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [a["id"] for a in saved["apps"]] == ["tray", "tray-2", "tray-3", "other"]


def test_migration_never_reuses_an_id_already_in_the_file(tmp_path: Path):
    path = tmp_path / "apps.json"
    _write_rows(
        path,
        [
            _row("tray", "C:/a/tray.bat"),
            _row("tray", "C:/b/tray.bat"),
            _row("tray-2", "C:/c/tray.bat"),
        ],
    )
    ids = [a.id for a in load_registry(path).apps]
    assert len(set(ids)) == 3
    assert ids[0] == "tray" and ids[2] == "tray-2"


def _ambiguous() -> Registry:
    return Registry(
        scan_root="",
        apps=[
            AppEntry(id="tray", name="Hub", kind="tray", bat_path="C:/hub/tray.bat"),
            AppEntry(id="tray", name="Stale", kind="tray", bat_path="C:/old/tray.bat"),
        ],
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda r: get_by_id(r, "tray"),
        lambda r: remove_by_id(r, "tray"),
        lambda r: rename_by_id(r, "tray", "New"),
        lambda r: set_autostart_by_id(r, "tray", True),
    ],
)
def test_by_id_helpers_refuse_an_ambiguous_id_and_change_nothing(call, monkeypatch, tmp_path):
    from src import registry as registry_mod

    monkeypatch.setattr(registry_mod, "DEFAULT_REGISTRY_PATH", tmp_path / "apps.json")
    reg = _ambiguous()
    with pytest.raises(AmbiguousAppId, match="tray"):
        call(reg)
    assert [a.name for a in reg.apps] == ["Hub", "Stale"]
    assert not (tmp_path / "apps.json").exists()
