"""Unit tests for this repo's diff-proportionate e2e routing declaration.

The mechanism is project-scaffolding's `scripts/classify_e2e.py`, vendored
byte-for-byte; its own suite proves the mechanism. These tests pin *this* repo's
declaration: they load the real `.fleet.toml` `[e2e]` table and assert that
representative paths land in the tier their rule intends, so an edit that silently
under-routes a real browser surface fails here. Pure path->tier classification; no
git, no browser. Also pins the vendored SVG sprite + pure-Python unit test diff that
motivated routing: it must route to the fast ``static`` / Chromium-only tier.
"""

from __future__ import annotations

import pathlib

import pytest

from scripts.classify_e2e import (
    Category,
    E2EConfig,
    _classify_one,
    classify,
    load_config,
)
from scripts.session_host_touched import session_host_touched

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
REAL_FLEET_TOML = REPO_ROOT / ".fleet.toml"


@pytest.fixture(scope="module")
def cfg() -> E2EConfig:
    return load_config(REAL_FLEET_TOML)


def test_real_fleet_toml_declares_usable_e2e_table(cfg: E2EConfig) -> None:
    """A table the classifier can't use would silently fail-safe every diff to full."""
    assert cfg.source == "declared"
    assert cfg.static_pytest_target == "tests/e2e/test_smoke.py"
    assert cfg.static_browsers == ("chromium",)
    assert cfg.full_pytest_target == "tests/e2e"
    assert not cfg.surfaces, "no surfaces declared: every full diff runs the whole suite"


# ------------------------------------------------------- per-file categories
@pytest.mark.parametrize(
    "path,expected",
    [
        # static assets under the webapp static dir
        ("app/webapp/static/_vendored/icons/icons-sprite.html", Category.STATIC),
        ("app/webapp/static/icon-512.png", Category.STATIC),
        ("app/webapp/static/favicon.ico", Category.STATIC),
        ("app/webapp/static/manifest.webmanifest", Category.STATIC),
        ("app/webapp/static/icons/foo.svg", Category.STATIC),
        # real browser surface -> FULL
        ("app/webapp/static/apps.js", Category.FULL),
        ("app/webapp/static/styles/base.css", Category.FULL),
        ("app/webapp/static/index.html", Category.FULL),          # real page, not vendored
        ("app/webapp/static/_vendored/nav/nav.css", Category.FULL),  # vendored CSS is layout
        ("app/webapp/server.py", Category.FULL),
        ("app/webapp/routers/board.py", Category.FULL),
        ("app/webapp/session_host/server.py", Category.FULL),
        ("src/session_host.py", Category.FULL),
        ("src/session_host_pty.py", Category.FULL),  # future split, not on disk yet
        ("src/session_client.py", Category.FULL),
        ("src/launcher.py", Category.FULL),
        ("scripts/shot_baseline.py", Category.FULL),  # its proof is a browser run
        ("tests/e2e/shots/coding-phone-light.png", Category.FULL),
        ("launcher.py", Category.FULL),
        ("tests/e2e/test_board_tab.py", Category.FULL),
        ("tests/conftest.py", Category.FULL),
        # no browser impact -> NONE
        ("src/board.py", Category.NONE),
        ("src/jobs.py", Category.NONE),
        ("tests/unit/tooling/test_classify_e2e.py", Category.NONE),
        ("tests/unit/tooling/test_icon_sprite_coverage.py", Category.NONE),
        ("docs/architecture.mmd", Category.NONE),
        ("README.md", Category.NONE),
        ("scripts/verify-before-ship.ps1", Category.NONE),
        ("config/apps.sample.json", Category.NONE),
        (".github/workflows/e2e.yml", Category.NONE),
        (".fleet.toml", Category.NONE),
        (".static-ratchet-baseline.json", Category.NONE),
        (".docs-ratchet-baseline.json", Category.NONE),
        (".comment-ratchet-baseline.json", Category.NONE),
        (".instruction-ratchet-baseline.json", Category.NONE),
        (".doc-refs-ratchet-baseline.json", Category.NONE),
        (".dead-code-ratchet-baseline.json", Category.NONE),
        (".conventions-ratchet-baseline.json", Category.NONE),
        ("tray.bat", Category.NONE),
        # unrecognized -> fail-safe FULL
        ("some/weird/new_dir/thing.xyz", Category.FULL),
        ("app/cli/commands/launch.py", Category.FULL),  # app/** off static -> full (safe)
    ],
)
def test_classify_one(cfg: E2EConfig, path: str, expected: Category) -> None:
    cat, _label = _classify_one(path, cfg.rules)
    assert cat is expected, f"{path} -> {cat.name}, expected {expected.name}"


# --------------------------------------------------------------- tier routing
def test_static_only_routes_to_chromium_smoke(cfg: E2EConfig) -> None:
    """The #565 diff: vendored sprite + one pure-Python unit test."""
    r = classify([
        "app/webapp/static/_vendored/icons/icons-sprite.html",
        "tests/unit/tooling/test_icon_sprite_coverage.py",
    ], cfg)
    assert r.tier == "static"
    assert r.browsers == ["chromium"]
    assert r.pytest_target == "tests/e2e/test_smoke.py"
    assert r.reasons  # non-empty: names the triggering path


def test_js_change_routes_to_full(cfg: E2EConfig) -> None:
    r = classify(["app/webapp/static/apps.js"], cfg)
    assert r.tier == "full"
    assert r.browsers == []  # empty = the suite's own default projections
    assert r.pytest_target == "tests/e2e"


def test_css_change_routes_to_full(cfg: E2EConfig) -> None:
    r = classify(["app/webapp/static/styles/base.css"], cfg)
    assert r.tier == "full"


def test_mixed_static_and_js_routes_to_full(cfg: E2EConfig) -> None:
    """Fail-safe: a static asset AND a .js file -> full suite, not narrow."""
    r = classify([
        "app/webapp/static/_vendored/icons/icons-sprite.html",
        "app/webapp/static/apps.js",
    ], cfg)
    assert r.tier == "full"


def test_backend_python_only_skips_browser(cfg: E2EConfig) -> None:
    r = classify(["src/board.py", "tests/unit/board/test_board_merge.py"], cfg)
    assert r.tier == "skip"
    assert r.pytest_target == ""


def test_docs_only_skips_browser(cfg: E2EConfig) -> None:
    r = classify(["README.md", "docs/architecture.mmd"], cfg)
    assert r.tier == "skip"


def test_unclassified_is_full(cfg: E2EConfig) -> None:
    r = classify(["random/thing.xyz"], cfg)
    assert r.tier == "full"


def test_empty_diff_is_full(cfg: E2EConfig) -> None:
    """No changed files -> can't prove narrow -> fail-safe full."""
    r = classify([], cfg)
    assert r.tier == "full"
    assert r.reasons


def test_backslash_paths_are_normalized(cfg: E2EConfig) -> None:
    r = classify(["app\\webapp\\static\\icon-512.png"], cfg)
    assert r.tier == "static"


def test_session_host_python_forces_full(cfg: E2EConfig) -> None:
    """A backend .py *on* the session-host path still gets full coverage."""
    r = classify(["src/session_host.py", "src/board.py"], cfg)
    assert r.tier == "full"


# --------------------------------------------------------- real-tree drift guard
def test_real_session_host_files_route_full(cfg: E2EConfig) -> None:
    """Every real `src/session_host*.py` on disk must classify FULL.

    Guards against layout drift: a new session-host module added on disk
    without the classifier being taught about it would silently narrow e2e
    coverage while every hand-written test above stays green. This test
    walks the real tree, so it fails loudly (naming the offending file) the
    moment that happens.
    """
    src_dir = REPO_ROOT / "src"
    session_host_files = sorted(src_dir.glob("session_host*.py"))
    assert session_host_files, "expected src/session_host.py to exist on disk"
    for f in session_host_files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        cat, _label = _classify_one(rel, cfg.rules)
        assert cat is Category.FULL, f"{rel} -> {cat.name}, expected FULL (layout drift?)"


def test_real_static_asset_routes_static(cfg: E2EConfig) -> None:
    """Sanity: a real image under app/webapp/static/ still classifies STATIC."""
    real_png = REPO_ROOT / "app/webapp/static/icon-512.png"
    assert real_png.is_file(), "fixture file moved/renamed; update this test"
    cat, _label = _classify_one("app/webapp/static/icon-512.png", cfg.rules)
    assert cat is Category.STATIC


def test_real_webapp_js_routes_full(cfg: E2EConfig) -> None:
    """Sanity: a real app/webapp/static/*.js file still classifies FULL."""
    real_js = REPO_ROOT / "app/webapp/static/apps.js"
    assert real_js.is_file(), "fixture file moved/renamed; update this test"
    cat, _label = _classify_one("app/webapp/static/apps.js", cfg.rules)
    assert cat is Category.FULL


# ------------------------------------------------ session-host touched warning
def test_session_host_touched_reads_the_declared_list(cfg: E2EConfig) -> None:
    """`src/vt_snapshot.py` routes to no browser tier yet is loaded by the
    session-host, so the gate's "not live after restart" warning must fire."""
    assert classify(["src/vt_snapshot.py"], cfg).tier == "skip"
    assert session_host_touched(["src/vt_snapshot.py"]) == "yes"
    assert session_host_touched(["src/board.py", "README.md"]) == "no"
