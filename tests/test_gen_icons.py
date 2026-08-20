"""Unit tests for the brand-asset re-sync tool (issue #28).

The fork commits its PWA/tray/Stream-Deck icons verbatim from upstream
`app-launcher` rather than generating them, so `scripts/gen_icons.py` is what
proves the committed copies still match. Pure filesystem work against a
synthetic upstream tree — no real checkout required, so this runs anywhere.
"""

from __future__ import annotations

import pathlib

import pytest

from scripts import gen_icons

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _fake_upstream(root: pathlib.Path) -> pathlib.Path:
    """A checkout-shaped tree carrying this repo's own brand assets."""
    for relative in gen_icons.BRAND_ASSETS:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPO_ROOT / relative).read_bytes())
    return root


# ------------------------------------------------------------ the asset list
def test_every_declared_brand_asset_is_committed():
    """BRAND_ASSETS must name files that actually exist — a typo would make
    the tool silently report `missing-local` forever."""
    missing = [rel for rel in gen_icons.BRAND_ASSETS if not (REPO_ROOT / rel).is_file()]
    assert missing == []


# ------------------------------------------------------------------ compare
def test_compare_reports_identical_for_a_matching_upstream(tmp_path):
    results = gen_icons.compare(_fake_upstream(tmp_path))
    assert [status for _, status in results] == [gen_icons.IDENTICAL] * len(
        gen_icons.BRAND_ASSETS
    )


def test_compare_flags_a_changed_asset_as_drift(tmp_path):
    upstream = _fake_upstream(tmp_path)
    changed = gen_icons.BRAND_ASSETS[0]
    (upstream / changed).write_bytes(b"a different rendering")

    statuses = dict(gen_icons.compare(upstream))
    assert statuses[changed] == gen_icons.DRIFT
    assert set(statuses.values()) == {gen_icons.DRIFT, gen_icons.IDENTICAL}


def test_compare_flags_an_asset_upstream_no_longer_ships(tmp_path):
    upstream = _fake_upstream(tmp_path)
    dropped = gen_icons.BRAND_ASSETS[-1]
    (upstream / dropped).unlink()

    statuses = dict(gen_icons.compare(upstream))
    assert statuses[dropped] == gen_icons.MISSING_UPSTREAM


# --------------------------------------------------------------------- sync
def test_sync_copies_only_the_drifted_assets(tmp_path):
    upstream = _fake_upstream(tmp_path / "upstream")
    local = _fake_upstream(tmp_path / "local")
    changed = gen_icons.BRAND_ASSETS[1]
    (upstream / changed).write_bytes(b"new brand art")

    results = gen_icons.compare(upstream, local)
    assert gen_icons.sync(upstream, results, local) == [changed]
    assert (local / changed).read_bytes() == b"new brand art"
    assert gen_icons.compare(upstream, local) == [
        (rel, gen_icons.IDENTICAL) for rel in gen_icons.BRAND_ASSETS
    ]


def test_sync_restores_a_locally_deleted_asset(tmp_path):
    upstream = _fake_upstream(tmp_path / "upstream")
    local = _fake_upstream(tmp_path / "local")
    dropped = gen_icons.BRAND_ASSETS[-1]
    (local / dropped).unlink()

    results = gen_icons.compare(upstream, local)
    assert dict(results)[dropped] == gen_icons.MISSING_LOCAL
    assert gen_icons.sync(upstream, results, local) == [dropped]
    assert (local / dropped).is_file()


# ---------------------------------------------------------------------- CLI
def test_main_exits_2_when_the_upstream_checkout_is_absent(tmp_path, capsys):
    assert gen_icons.main(["--upstream", str(tmp_path / "nope")]) == 2
    assert "not found" in capsys.readouterr().err


def test_main_exits_0_on_a_matching_upstream(tmp_path, capsys):
    upstream = _fake_upstream(tmp_path)
    assert gen_icons.main(["--upstream", str(upstream)]) == 0
    assert "all 7 brand assets match" in capsys.readouterr().out


def test_main_exits_1_on_drift_without_sync(tmp_path, capsys):
    upstream = _fake_upstream(tmp_path)
    (upstream / gen_icons.BRAND_ASSETS[0]).write_bytes(b"drifted")

    assert gen_icons.main(["--upstream", str(upstream)]) == 1
    captured = capsys.readouterr()
    assert gen_icons.DRIFT in captured.out
    assert "--sync" in captured.err


def test_default_upstream_dir_honours_the_env_override(monkeypatch):
    monkeypatch.setenv("APP_LAUNCHER_DIR", r"D:\code\app-launcher")
    assert gen_icons.default_upstream_dir() == pathlib.Path(r"D:\code\app-launcher")

    monkeypatch.setenv("APP_LAUNCHER_DIR", "  ")
    assert gen_icons.default_upstream_dir() == REPO_ROOT.parent / "app-launcher"
