"""The page's ES modules as a handful of responses.

``index.html`` loads ``main.js``, whose boot graph is about 60 modules. Over
HTTP/2 on the phone profile each module is its own response, and responses
finish about 15 ms apart whatever their size, so the module count, not the
bytes, sets when the Board draws. At server start the graph is bundled with a
pinned esbuild into one entry plus shared chunks, and every module
``lazy-views.js`` or ``main.js`` imports with ``import()`` becomes its own
chunk that still loads on first open. Contributors run no build step: the
server builds, as it does ``/static/bundle.css``.

The source files stay the source of truth. A build is cached on disk under
its key (the fleet hash, so any module edit rebuilds) and a restart with no
asset change reads the cache. When Node or esbuild is unavailable the build
raises and the page loads the separate modules, so a failed build costs
speed, not function.

Each served file carries ``?v=<fleet hash>`` on its URL, chunk imports
included: the vendored build watch reads its own stamp from
``import.meta.url``, and a chunk loaded under two URLs would be two module
instances. esbuild's metafile records which chunk each source module became
and what each chunk imports, so ``index.html``'s modulepreload markers name
the chunks a boot needs instead of the source modules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from src import runtime_files
from src.subprocess_flags import NO_WINDOW

_log = logging.getLogger(__name__)

ENTRY = "main.js"
BUNDLE_DIR = "js-bundle"
# Pinned exactly: the bundle's shape is part of the build, so an upgrade is a reviewed change.
ESBUILD_VERSION = "0.24.0"
_METAFILE = "meta.json"
_ESBUILD_FLAGS = (
    "--bundle",
    "--splitting",
    "--format=esm",
    "--sourcemap",
    "--minify-whitespace",
    "--minify-syntax",
    "--log-level=error",
)
# First use downloads esbuild into the npm cache; a cached run takes ~2 s.
BUILD_TIMEOUT_S = 120
_COMPLETE_MARKER = ".complete"

# A page load carrying this cookie set to ``off`` gets the separate modules:
# a test that imports a live module by its URL needs the page to share it.
UNBUNDLED_COOKIE = "launcher_js_bundle"

_ENTRY_SCRIPT_RE = re.compile(r"""(<script type="module" src=")/static/main\.js(?:\?v=[^"]*)?(")""")

_MEDIA_TYPES = {".js": "text/javascript", ".map": "application/json"}

Runner = Callable[[Path, Path], None]


class BundleError(RuntimeError):
    """The bundler could not produce a bundle (no Node, a failed or timed-out build)."""


class JsBundle(NamedTuple):
    """A built bundle: its served files and the chunk graph its metafile records."""

    files: Dict[str, Tuple[bytes, str]]
    # Static-dir-relative source module -> the output file it became.
    entries: Dict[str, str]
    # Output file -> the output files it imports statically.
    imports: Dict[str, List[str]]

    def closure(self, module: str) -> List[str]:
        """The output files a browser fetches before ``module`` can run, entry first.

        Empty when ``module`` is not an entry of the bundle (neither ``main.js``
        nor a module some code imports with ``import()``).
        """
        start = self.entries.get(posixpath.normpath(module))
        order: List[str] = []
        queue = [start] if start else []
        while queue:
            name = queue.pop(0)
            if name in order:
                continue
            order.append(name)
            queue += self.imports.get(name, [])
        return order


def link_js_bundle(shell: str) -> str:
    """``shell`` with its ``main.js`` entry script pointed at the bundle's entry."""
    return _ENTRY_SCRIPT_RE.sub(rf"\g<1>/static/{BUNDLE_DIR}/{ENTRY}\g<2>", shell)


def bundle_key(fleet_hash: str) -> str:
    """The cache key of a build: the sources' fleet hash plus how they are bundled."""
    recipe = "\n".join([fleet_hash, ESBUILD_VERSION, *_ESBUILD_FLAGS])
    return hashlib.sha256(recipe.encode("utf-8")).hexdigest()[:16]


def run_esbuild(static_dir: Path, out_dir: Path) -> None:
    """Bundle ``static_dir/main.js`` into ``out_dir`` with the pinned esbuild."""
    npx = shutil.which("npx")
    if npx is None:
        raise BundleError("npx not found on PATH (Node.js is not installed)")
    cmd = [npx, "--yes", "--prefer-offline", f"esbuild@{ESBUILD_VERSION}", ENTRY,
           *_ESBUILD_FLAGS, f"--outdir={out_dir}", f"--metafile={out_dir / _METAFILE}"]
    try:
        done = subprocess.run(cmd, cwd=static_dir, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=BUILD_TIMEOUT_S, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise BundleError(f"esbuild did not finish within {BUILD_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise BundleError(f"could not run npx: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[-2000:]
        raise BundleError(f"esbuild exited {done.returncode}: {detail}")


def _remove_tree(path: Path) -> None:
    shutil.rmtree(runtime_files.checked(path), ignore_errors=True)


def _build_into_cache(static_dir: Path, cache_root: Path, key: str, run: Runner) -> Path:
    """The cache folder holding the build for ``key``, building it first when absent."""
    dest = cache_root / key
    if (dest / _COMPLETE_MARKER).is_file():
        return dest
    runtime_files.make_dirs(cache_root)
    tmp = cache_root / f"{key}.tmp-{os.getpid()}"
    _remove_tree(tmp)
    try:
        run(static_dir, tmp)
        for required in (ENTRY, _METAFILE):
            if not (tmp / required).is_file():
                raise BundleError(f"esbuild wrote no {required}")
        runtime_files.save_text(tmp / _COMPLETE_MARKER, key)
        if not (dest / _COMPLETE_MARKER).is_file():
            _remove_tree(dest)
            try:
                runtime_files.replace(tmp, dest)
            except OSError:
                # Another process starting from the same sources finished first.
                if not (dest / _COMPLETE_MARKER).is_file():
                    raise
    finally:
        _remove_tree(tmp)
    for stale in cache_root.iterdir():
        if stale.name != key and ".tmp-" not in stale.name:
            _remove_tree(stale)
    return dest


def _stamp_chunks(body: str, names: List[str], fleet_hash: str) -> str:
    """Every ``"./<output>.js"`` specifier in ``body`` with ``?v=<fleet hash>``, bare imports included."""
    for name in names:
        body = body.replace(f'"./{name}"', f'"./{name}?v={fleet_hash}"')
    return body


def _map_sources_to_static(body: str, out_dir: Path, static_dir: Path) -> str:
    """A source map whose ``sources`` name the served ``/static/`` paths, not the cache folder's."""
    doc = json.loads(body)
    sources = []
    for src in doc.get("sources", []):
        try:
            rel = (out_dir / src).resolve().relative_to(static_dir.resolve()).as_posix()
            sources.append(f"/static/{rel}")
        except ValueError:
            sources.append(src)
    doc["sources"] = sources
    return json.dumps(doc)


def _chunk_graph(meta_text: str) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """``(entries, imports)`` from esbuild's metafile, keyed by output file name.

    The metafile names outputs relative to esbuild's working folder (the cache
    folder's path from ``static_dir``); a bundle is one flat folder, so the
    base name is the output's served name.
    """
    outputs = json.loads(meta_text).get("outputs", {})
    entries: Dict[str, str] = {}
    imports: Dict[str, List[str]] = {}
    for path, out in outputs.items():
        name = posixpath.basename(path)
        if not name.endswith(".js"):
            continue
        if out.get("entryPoint"):
            entries[posixpath.normpath(out["entryPoint"])] = name
        imports[name] = [posixpath.basename(dep["path"]) for dep in out.get("imports", [])
                         if dep.get("kind") == "import-statement" and not dep.get("external")]
    return entries, imports


def build_js_bundle(static_dir: Path, cache_root: Path, fleet_hash: str,
                    run: Runner = run_esbuild) -> Optional[JsBundle]:
    """The bundle of ``static_dir/main.js``, or ``None`` when there is nothing to bundle.

    ``None`` when there is no entry or no fleet hash. Raises ``BundleError``,
    ``OSError`` or ``ValueError`` when the build fails; the caller serves the
    separate modules then.
    """
    if not fleet_hash or not (static_dir / ENTRY).is_file():
        return None
    out_dir = _build_into_cache(static_dir, cache_root, bundle_key(fleet_hash), run)
    entries, imports = _chunk_graph((out_dir / _METAFILE).read_text(encoding="utf-8"))
    files = sorted(p for p in out_dir.iterdir() if p.is_file() and p.suffix in _MEDIA_TYPES)
    js_names = [p.name for p in files if p.suffix == ".js"]
    served: Dict[str, Tuple[bytes, str]] = {}
    for path in files:
        body = path.read_text(encoding="utf-8")
        if path.suffix == ".js":
            body = _stamp_chunks(body, js_names, fleet_hash)
        else:
            body = _map_sources_to_static(body, out_dir, static_dir)
        served[path.name] = (body.encode("utf-8"), _MEDIA_TYPES[path.suffix])
    return JsBundle(served, entries, imports)
