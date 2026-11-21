"""Deterministic front-end for the lite `/e2e` skill.

A downscaled form of `fleet-config`'s `skills/_lib/e2e_route.py`
with one deliberate difference: this copy depends on no external checkout. The
self-healing bootstrap sources the `classify_e2e.py` **bundled next to this
script**, so `skills/e2e/` plus its sibling `skills/_lib/` (the guarded writer
imported below) propagate to any machine as-is.

The routing mechanism is the bundled classifier: a target repo's own
`scripts/classify_e2e.py` reads that repo's `.fleet.toml` `[e2e]` table and
maps the changed-file set to a tier — `skip` / `static` / `full`, fail-safe to
`full` on anything unmatched, malformed, or empty. This module never
re-implements the classification; it locates, runs, boots, and reports.

Subcommands (all print `KEY=value` lines for the skill to read):

  probe <repo-root>       facts: CLASSIFIER / CLASSIFIER_MATCHES_BUNDLED /
                          E2E_TABLE / SUITE / WEB_SURFACE / WEB_KIND / WEB_REASON
  route <repo-root> [f..] run the repo's classifier, pass E2E_* through
                          verbatim; absent -> SOURCE=judgment + tier unknown;
                          error -> SOURCE=classifier-error + tier full
  bootstrap <repo-root>   copy the bundled classifier into the repo
                          byte-verbatim (refuses a diverged custom one
                          without --force); never writes .fleet.toml

Stdlib only, Python 3.11+ (tomllib).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_lib"))
from runtime_files import make_dirs, save_bytes  # noqa: E402

BUNDLED_CLASSIFIER = Path(__file__).resolve().parent / "classify_e2e.py"
CLASSIFIER_REL = Path("scripts/classify_e2e.py")
SUITE_REL = Path("tests/e2e")
_WEB_DEP = re.compile(r"\b(fastapi|flask|uvicorn|starlette)\b", re.IGNORECASE)
_STREAMLIT_DEP = re.compile(r"\bstreamlit\b", re.IGNORECASE)

# Suppress console flashes when a console-less parent (a tray, a scheduled
# task) spawns us on Windows; harmless 0 elsewhere.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _sha1(path: Path) -> Optional[str]:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def classifier_state(repo: Path, source: Path) -> Tuple[str, str]:
    """`(CLASSIFIER, CLASSIFIER_MATCHES_BUNDLED)` for the probe output."""
    own = _sha1(repo / CLASSIFIER_REL)
    if own is None:
        return "absent", "n/a"
    ref = _sha1(source)
    if ref is None:
        return "present", "n/a"
    return "present", "yes" if own == ref else "no"


def e2e_table_state(repo: Path) -> str:
    """`present` / `absent` / `invalid` for the `.fleet.toml` `[e2e]` table."""
    fleet_toml = repo / ".fleet.toml"
    if not fleet_toml.is_file():
        return "absent"
    import tomllib
    try:
        data = tomllib.loads(fleet_toml.read_text(encoding="utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        return "invalid"
    return "present" if isinstance(data.get("e2e"), dict) else "absent"


def suite_state(repo: Path) -> str:
    suite = repo / SUITE_REL
    if not suite.is_dir():
        return "absent"
    return "present" if any(suite.rglob("test_*.py")) else "absent"


def _dependency_text(repo: Path) -> str:
    chunks: List[str] = []
    for req in sorted(repo.glob("requirements*.txt")):
        try:
            chunks.append(req.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            chunks.append(pyproject.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(chunks)


def _fleet_layer(repo: Path) -> Optional[str]:
    fleet_toml = repo / ".fleet.toml"
    if not fleet_toml.is_file():
        return None
    import tomllib
    try:
        data = tomllib.loads(fleet_toml.read_text(encoding="utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        return None
    layer = data.get("layer")
    return layer if isinstance(layer, str) else None


def detect_web_surface(repo: Path) -> Tuple[str, str, str]:
    """`(WEB_SURFACE, WEB_KIND, WEB_REASON)` — declared layer first, then
    dependency heuristics. Non-web repos read `no`, which keeps the skill's
    "worth adding a suite?" evaluation off them by construction."""
    if _fleet_layer(repo) == "working-web":
        return "yes", "webapp", ".fleet.toml layer=working-web"
    deps = _dependency_text(repo)
    if _STREAMLIT_DEP.search(deps) or (repo / "streamlit_app.py").is_file():
        return "yes", "streamlit", "streamlit dependency/entrypoint"
    m = _WEB_DEP.search(deps)
    if m:
        return "yes", "webapp", f"{m.group(1).lower()} dependency"
    return "no", "none", "no web framework signal"


def files_identical(a: Path, b: Path) -> bool:
    ha, hb = _sha1(a), _sha1(b)
    return ha is not None and ha == hb


def cmd_probe(repo: Path, source: Path) -> int:
    classifier, matches = classifier_state(repo, source)
    web, kind, reason = detect_web_surface(repo)
    print(f"CLASSIFIER={classifier}")
    print(f"CLASSIFIER_MATCHES_BUNDLED={matches}")
    print(f"E2E_TABLE={e2e_table_state(repo)}")
    print(f"SUITE={suite_state(repo)}")
    print(f"SUITE_DIR={SUITE_REL.as_posix()}")
    print(f"WEB_SURFACE={web}")
    print(f"WEB_KIND={kind}")
    print(f"WEB_REASON={reason}")
    return 0


def cmd_route(repo: Path, files: List[str]) -> int:
    classifier = repo / CLASSIFIER_REL
    if not classifier.is_file():
        print("SOURCE=judgment")
        print("E2E_TIER=unknown")
        print("E2E_REASON=no classifier - judgment layer decides (fail-safe: full)")
        return 0
    try:
        res = subprocess.run(
            [sys.executable, str(classifier), *files],
            cwd=str(repo), capture_output=True, text=True,
            timeout=120, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("SOURCE=classifier-error")
        print("E2E_TIER=full")
        print(f"E2E_REASON=classifier failed to run ({type(exc).__name__}) - fail-safe full")
        return 0
    e2e_lines = [ln for ln in res.stdout.splitlines() if ln.startswith("E2E_")]
    if res.returncode != 0 or not any(ln.startswith("E2E_TIER=") for ln in e2e_lines):
        print("SOURCE=classifier-error")
        print("E2E_TIER=full")
        print(f"E2E_REASON=classifier exit {res.returncode} without a tier - fail-safe full")
        return 0
    print("SOURCE=classifier")
    for ln in e2e_lines:
        print(ln)
    return 0


def cmd_bootstrap(repo: Path, source: Path, force: bool) -> int:
    if not source.is_file():
        print(f"BOOTSTRAP=error REASON=bundled classifier not found at {source}")
        return 1
    dest = repo / CLASSIFIER_REL
    if dest.is_file():
        if files_identical(source, dest):
            print("BOOTSTRAP=exists-identical")
            print(f"DEST={dest}")
            return 0
        if not force:
            print("BOOTSTRAP=refused REASON=existing classifier differs from the bundled "
                  "one (custom implementation - migrate deliberately with --force)")
            return 1
    try:
        make_dirs(dest.parent)
        save_bytes(dest, source.read_bytes())
    except ValueError as exc:
        print(f"BOOTSTRAP=refused REASON={exc}")
        return 1
    if not files_identical(source, dest):
        print("BOOTSTRAP=error REASON=post-copy verification failed (bytes differ)")
        return 1
    print("BOOTSTRAP=copied")
    print(f"DEST={dest}")
    print(f"SHA={_sha1(dest)}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic front-end for the lite /e2e skill.")
    ap.add_argument("--source", type=Path, default=BUNDLED_CLASSIFIER,
                    help="classifier to bootstrap from (default: the bundled copy)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="repo facts: classifier/table/suite/web-surface")
    p_probe.add_argument("repo", type=Path)

    p_route = sub.add_parser("route", help="run the repo's classifier on the live diff")
    p_route.add_argument("repo", type=Path)
    p_route.add_argument("files", nargs="*", help="explicit file list (default: live diff)")

    p_boot = sub.add_parser("bootstrap", help="copy the bundled classifier in, byte-verbatim")
    p_boot.add_argument("repo", type=Path)
    p_boot.add_argument("--force", action="store_true",
                        help="overwrite an existing, different classifier")

    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"Not a directory: {repo}", file=sys.stderr)
        return 2
    if args.cmd == "probe":
        return cmd_probe(repo, args.source)
    if args.cmd == "route":
        return cmd_route(repo, args.files)
    return cmd_bootstrap(repo, args.source, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
