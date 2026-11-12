"""Unit tests for the ``scan`` CLI subcommand.

``discover_new`` runs for real (only the bat walk and config/registry
persistence are stubbed), so a signature mismatch between the command and
``src.registry`` fails here instead of at the user's terminal.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from app.cli.commands import scan_cmd
from app.cli.commands.scan_cmd import ScanCommand
from src.registry import Registry


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    root = tmp_path / "apps"
    bat = root / "demo" / "run.bat"
    bat.parent.mkdir(parents=True)
    bat.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(
        scan_cmd, "load_webapp_config", lambda: SimpleNamespace(apps_scan_root=str(root), projects_dir=str(tmp_path))
    )
    monkeypatch.setattr(scan_cmd, "load_registry", lambda: Registry(scan_root=str(root)))
    monkeypatch.setattr("src.registry.scan_app_bats", lambda _root: [(bat, "streamlit")])
    persisted: list = []
    monkeypatch.setattr(
        scan_cmd,
        "persist_additions",
        lambda reg, new, root_: persisted.extend(new) or list(new),
    )
    return persisted


def _run(dry_run: bool) -> int:
    return ScanCommand(config=None).execute(argparse.Namespace(dry_run=dry_run))


def test_scan_persists_new_entries(scan_env):
    assert _run(dry_run=False) == 0
    assert [e.kind for e in scan_env] == ["streamlit"]


def test_scan_dry_run_persists_nothing(scan_env):
    assert _run(dry_run=True) == 0
    assert scan_env == []
