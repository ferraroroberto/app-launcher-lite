"""DirListingCache: an unchanged folder costs a stat per folder, never a re-read of the listing.

Synthetic folders under ``tmp_path`` only.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List

import pytest

from app.webapp.routers import team_os
from src.life_os import stat_cache
from src.life_os.stat_cache import DirListingCache


class _Builds:
    """A build function that counts how often it ran."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.count = 0

    def __call__(self) -> List[str]:
        self.count += 1
        return sorted(p.name for p in self.root.rglob("*") if p.is_file())


def _settle(*folders: Path) -> None:
    """Age each folder's mtime past the racy window, as a folder nobody touched for a while."""
    old = time.time_ns() - 60_000_000_000
    for folder in folders:
        os.utime(folder, ns=(old, old))


def test_unchanged_tree_costs_one_stat_per_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.md").write_text("x", encoding="utf-8")
    _settle(tmp_path, tmp_path / "sub")
    cache = DirListingCache()
    build = _Builds(tmp_path)
    cache.get(tmp_path, "k", build, recursive=True)
    seen: List[str] = []
    real = os.stat

    def counting(path, *args, **kwargs):
        seen.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(stat_cache.os, "stat", counting)

    assert cache.get(tmp_path, "k", build, recursive=True) == ["x.md"]

    assert build.count == 1
    assert sorted(seen) == sorted([str(tmp_path), str(tmp_path / "sub")])


def test_a_new_file_in_a_nested_folder_rebuilds(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    cache = DirListingCache()
    build = _Builds(tmp_path)
    cache.get(tmp_path, "k", build, recursive=True)
    (tmp_path / "sub" / "deep.md").write_text("d", encoding="utf-8")
    assert cache.get(tmp_path, "k", build, recursive=True) == ["deep.md"]
    assert build.count == 2


def test_a_change_in_the_same_mtime_tick_is_not_missed(tmp_path: Path) -> None:
    """A coarse clock leaves the folder mtime unchanged by a second write in the same tick."""
    tick = time.time_ns()
    os.utime(tmp_path, ns=(tick, tick))
    cache = DirListingCache()
    build = _Builds(tmp_path)
    assert cache.get(tmp_path, "k", build) == []
    (tmp_path / "same_tick.md").write_text("s", encoding="utf-8")
    os.utime(tmp_path, ns=(tick, tick))
    assert cache.get(tmp_path, "k", build) == ["same_tick.md"]


def test_keys_keep_listings_of_one_folder_apart(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    _settle(tmp_path)
    cache = DirListingCache()
    assert cache.get(tmp_path, "one", lambda: ["one"]) == ["one"]
    assert cache.get(tmp_path, "two", lambda: ["two"]) == ["two"]
    assert cache.get(tmp_path, "one", lambda: ["rebuilt"]) == ["one"]


def test_walk_files_reads_an_unchanged_tree_once_and_hands_out_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skill"
    (skill / "memory").mkdir(parents=True)
    (skill / "SKILL.md").write_text("s", encoding="utf-8")
    (skill / "memory" / "obs.md").write_text("o", encoding="utf-8")
    _settle(skill, skill / "memory")
    monkeypatch.setattr(team_os, "_walk_cache", DirListingCache())
    reads: List[Path] = []
    real = team_os._walk_files

    def counting(root, team_os_root, category):
        reads.append(root)
        return real(root, team_os_root, category)

    monkeypatch.setattr(team_os, "_walk_files", counting)

    first = team_os.walk_files(skill, tmp_path.resolve(), None)
    first[0]["name"] = "mutated"
    second = team_os.walk_files(skill, tmp_path.resolve(), None)

    assert len(reads) == 1
    assert [f["name"] for f in second] != [f["name"] for f in first]
    assert {f["category"] for f in second} == {"memory", "skill"}

    (skill / "memory" / "new.md").write_text("n", encoding="utf-8")
    third = team_os.walk_files(skill, tmp_path.resolve(), None)
    assert len(reads) == 2
    assert "new.md" in {f["name"] for f in third}
