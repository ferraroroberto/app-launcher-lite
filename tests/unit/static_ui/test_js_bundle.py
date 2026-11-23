"""The ES-module bundle: built once per source state, every served URL stamped, its chunks preloaded."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webapp import server as server_mod
from app.webapp.module_preload import expand_preloads
from app.webapp.routers.misc import _bundle_closure
from src.static_bundles.js_bundle import (
    BUNDLE_DIR,
    BundleError,
    JsBundle,
    build_js_bundle,
    bundle_key,
    link_js_bundle,
)
from src.static_versioning import compute_asset_hashes, fleet_hash_of

_FLEET = "0123abcd"


def _tree(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    (static / "views").mkdir(parents=True)
    (static / "main.js").write_text(
        "import { a } from './a.js';\nimport('./views/lazy.js').then((m) => m.run(a));\n", encoding="utf-8")
    (static / "a.js").write_text("export const a = import.meta.url;\n", encoding="utf-8")
    (static / "views" / "lazy.js").write_text(
        "import { a } from '../a.js';\nexport function run(x) { return [a, x]; }\n", encoding="utf-8")
    return static


def _fake_esbuild(calls: list):
    """Writes what esbuild writes for ``_tree``: an entry, a shared chunk, a lazy chunk, a map, a metafile."""

    def run(static_dir: Path, out_dir: Path) -> None:
        calls.append(out_dir)
        out_dir.mkdir(parents=True)
        (out_dir / "main.js").write_text(
            'import {\n  a\n} from "./chunk-AAAA.js";\nimport("./lazy-BBBB.js");\n'
            "//# sourceMappingURL=main.js.map\n", encoding="utf-8")
        (out_dir / "chunk-AAAA.js").write_text("var a = import.meta.url;\nexport { a };\n", encoding="utf-8")
        (out_dir / "lazy-BBBB.js").write_text('import "./chunk-AAAA.js";\n', encoding="utf-8")
        rel = Path("..") / ".." / static_dir.name / "main.js"
        (out_dir / "main.js.map").write_text(
            json.dumps({"version": 3, "sources": [rel.as_posix()], "mappings": ""}), encoding="utf-8")
        out = "../cache/x"
        static_import = {"kind": "import-statement", "path": f"{out}/chunk-AAAA.js"}
        (out_dir / "meta.json").write_text(json.dumps({"outputs": {
            f"{out}/main.js": {"entryPoint": "main.js", "imports": [
                static_import, {"kind": "dynamic-import", "path": f"{out}/lazy-BBBB.js"}]},
            f"{out}/lazy-BBBB.js": {"entryPoint": "views/lazy.js", "imports": [static_import]},
            f"{out}/chunk-AAAA.js": {"imports": []},
            f"{out}/main.js.map": {"imports": []},
        }}), encoding="utf-8")

    return run


def _build(tmp_path: Path, calls: Optional[list] = None) -> JsBundle:
    return build_js_bundle(_tree(tmp_path), tmp_path / "cache", _FLEET, run=_fake_esbuild(calls if calls is not None else []))


def test_link_js_bundle_points_the_entry_script_at_the_bundle() -> None:
    shell = '<script src="/static/vendor/x.js"></script>\n<script type="module" src="/static/main.js?v=12345678"></script>\n'
    assert link_js_bundle(shell) == (
        '<script src="/static/vendor/x.js"></script>\n<script type="module" src="/static/js-bundle/main.js"></script>\n')


def test_a_changed_module_busts_the_cache(tmp_path: Path) -> None:
    """Any module edit moves the fleet hash, so the bundle's URL stamp and its cache key both move."""
    static = _tree(tmp_path)
    before = fleet_hash_of(compute_asset_hashes(static))
    (static / "views" / "lazy.js").write_text("export function run() { return 1; }\n", encoding="utf-8")
    after = fleet_hash_of(compute_asset_hashes(static))
    assert bundle_key(after) != bundle_key(before)


def test_every_chunk_import_carries_the_stamp_and_maps_name_the_served_sources(tmp_path: Path) -> None:
    files = _build(tmp_path).files
    main = files["main.js"][0].decode()
    assert f'from "./chunk-AAAA.js?v={_FLEET}"' in main
    assert f'import("./lazy-BBBB.js?v={_FLEET}")' in main
    # A bare import is a load too: unstamped, the chunk would load twice under two URLs.
    assert files["lazy-BBBB.js"][0].decode() == f'import "./chunk-AAAA.js?v={_FLEET}";\n'
    assert json.loads(files["main.js.map"][0])["sources"] == ["/static/main.js"]
    assert "meta.json" not in files


def test_the_chunk_graph_preloads_what_each_entry_needs(tmp_path: Path) -> None:
    """A lazy view's markers name its chunk and the shared chunks, never another entry's."""
    bundle = _build(tmp_path)
    assert bundle.closure("main.js") == ["main.js", "chunk-AAAA.js"]
    assert bundle.closure("./views/lazy.js") == ["lazy-BBBB.js", "chunk-AAAA.js"]
    assert bundle.closure("a.js") == []
    shell = "<template>\n  <!-- @modulepreload views/lazy.js -main.js -->\n</template>"
    assert expand_preloads(shell, tmp_path, _bundle_closure(bundle)) == (
        f'<template>\n  <link rel="modulepreload" href="/static/{BUNDLE_DIR}/lazy-BBBB.js">\n</template>')


def test_a_restart_on_the_same_sources_reads_the_cache(tmp_path: Path) -> None:
    calls = []
    first = _build(tmp_path, calls)
    assert build_js_bundle(tmp_path / "static", tmp_path / "cache", _FLEET, run=_fake_esbuild(calls)) == first
    assert len(calls) == 1
    build_js_bundle(tmp_path / "static", tmp_path / "cache", "feedf00d", run=_fake_esbuild(calls))
    assert len(calls) == 2
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == [bundle_key("feedf00d")]


def test_a_failed_build_leaves_no_cache_and_serves_the_separate_modules(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(static_dir: Path, out_dir: Path) -> None:
        out_dir.mkdir(parents=True)
        raise BundleError("esbuild exited 1")

    with pytest.raises(BundleError):
        build_js_bundle(_tree(tmp_path), tmp_path / "cache", _FLEET, run=broken)
    assert list((tmp_path / "cache").iterdir()) == []
    assert build_js_bundle(tmp_path, tmp_path / "none", _FLEET, run=broken) is None  # no entry

    def no_node(*args: object) -> None:
        raise BundleError("npx not found on PATH")

    monkeypatch.setattr(server_mod, "build_js_bundle", no_node)
    app = FastAPI()
    app.state.asset_fleet_hash = _FLEET
    hashes = {"main.js": _FLEET}
    assert server_mod._load_js_bundle(app, hashes) is None
    assert list(hashes) == ["main.js"]


def test_the_server_serves_the_bundle_and_404s_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _build(tmp_path)
    monkeypatch.setattr(server_mod, "build_js_bundle", lambda *args: bundle)
    app = FastAPI()
    app.state.asset_fleet_hash = _FLEET
    hashes = {"main.js": _FLEET}
    assert server_mod._load_js_bundle(app, hashes) is bundle
    assert hashes[f"{BUNDLE_DIR}/chunk-AAAA.js"] == _FLEET
    client = TestClient(app)
    resp = client.get(f"/static/{BUNDLE_DIR}/main.js")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/javascript")
    assert "immutable" in resp.headers["cache-control"]
    assert client.get(f"/static/{BUNDLE_DIR}/nope.js").status_code == 404


@pytest.mark.skipif(shutil.which("npx") is None, reason="Node.js is not installed")
def test_the_pinned_esbuild_bundles_a_lazy_view_as_its_own_chunk(tmp_path: Path) -> None:
    """The real bundler, with the server's flags: a lazy chunk, maps, a chunk graph, nothing unstamped."""
    bundle = build_js_bundle(_tree(tmp_path), tmp_path / "cache", _FLEET)
    names = sorted(bundle.files)
    assert "main.js" in names and "main.js.map" in names
    lazy = bundle.entries["views/lazy.js"]
    assert lazy in names and lazy != "main.js"
    assert lazy not in bundle.closure("main.js")
    for name, (body, _) in bundle.files.items():
        if name.endswith(".js"):
            for other in names:
                assert f'"./{other}"' not in body.decode(), f"{name} imports {other} unstamped"
