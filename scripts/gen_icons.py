"""Verify / re-sync the brand asset family against the upstream app-launcher.

This fork does **not** generate its icons. Upstream `app-launcher` builds them
from `project-scaffolding`'s shared `brand_gen.render_set()` generator; the lite
fork exists precisely to avoid carrying a `project-scaffolding` dependency, so
the rendered assets are committed here byte-for-byte instead. That is a
deliberate, permanent decision — see `CLAUDE.md`'s "accepted exceptions" block,
which is why the fleet design lint's `app-icon-family` contract FAILs by design
here and must not be re-filed as drift (issues #10, #28).

What was missing is the other half of that decision: nothing checked that the
committed copies still *match* upstream. `CLAUDE.md` prescribes "re-sync by
copying those files from an upstream checkout when its brand changes", and this
script is that procedure, executable:

    python scripts/gen_icons.py            # verify (exit 1 on drift)
    python scripts/gen_icons.py --sync     # copy the drifted assets over
    python scripts/gen_icons.py --upstream D:/code/app-launcher

The upstream checkout is resolved as this repo's sibling `../app-launcher` — the
same sibling convention `src/webapp_config.py` uses for its own peer checkouts —
overridable with `--upstream` or the `APP_LAUNCHER_DIR` environment variable.
Stdlib only, no Pillow, no generator import.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Every asset upstream's ``brand_gen.render_set()`` emits, repo-relative.
#: Keep in step with ``CLAUDE.md``'s accepted-exception block.
BRAND_ASSETS: Tuple[str, ...] = (
    "app/webapp/static/icon-180.png",
    "app/webapp/static/icon-192.png",
    "app/webapp/static/icon-512.png",
    "app/webapp/static/icon-512-maskable.png",
    "app/webapp/static/favicon.ico",
    "assets/tray/app-launcher.ico",
    "assets/stream-deck/app-launcher-144.png",
)

IDENTICAL = "identical"
DRIFT = "drift"
MISSING_LOCAL = "missing-local"
MISSING_UPSTREAM = "missing-upstream"


def default_upstream_dir() -> Path:
    """The upstream ``app-launcher`` checkout supplying the rendered assets."""
    override = os.environ.get("APP_LAUNCHER_DIR", "").strip()
    return Path(override) if override else PROJECT_ROOT.parent / "app-launcher"


def digest(path: Path) -> Optional[str]:
    """SHA-256 of ``path``, or ``None`` when it does not exist."""
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(upstream_root: Path, local_root: Path = PROJECT_ROOT) -> List[Tuple[str, str]]:
    """Compare every brand asset in ``local_root`` against ``upstream_root``.

    Returns one ``(relative_path, status)`` pair per asset, in
    :data:`BRAND_ASSETS` order.
    """
    results: List[Tuple[str, str]] = []
    for relative in BRAND_ASSETS:
        local_digest = digest(local_root / relative)
        upstream_digest = digest(upstream_root / relative)
        if upstream_digest is None:
            status = MISSING_UPSTREAM
        elif local_digest is None:
            status = MISSING_LOCAL
        elif local_digest == upstream_digest:
            status = IDENTICAL
        else:
            status = DRIFT
        results.append((relative, status))
    return results


def sync(
    upstream_root: Path,
    results: List[Tuple[str, str]],
    local_root: Path = PROJECT_ROOT,
) -> List[str]:
    """Copy every drifted / locally-missing asset over from upstream.

    Returns the relative paths actually written.
    """
    copied: List[str] = []
    for relative, status in results:
        if status not in (DRIFT, MISSING_LOCAL):
            continue
        destination = local_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upstream_root / relative, destination)
        copied.append(relative)
    return copied


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify (or re-sync) this fork's committed brand assets against the "
            "upstream app-launcher checkout that generates them."
        )
    )
    parser.add_argument(
        "--upstream",
        type=Path,
        default=None,
        help="upstream app-launcher checkout (default: ../app-launcher, or $APP_LAUNCHER_DIR)",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="copy drifted assets from upstream instead of only reporting them",
    )
    args = parser.parse_args(argv)

    upstream_root = args.upstream or default_upstream_dir()
    if not upstream_root.is_dir():
        print(
            f"❌ upstream app-launcher checkout not found at {upstream_root} — clone it "
            "beside this repo, pass --upstream, or set APP_LAUNCHER_DIR.",
            file=sys.stderr,
        )
        return 2

    results = compare(upstream_root)
    for relative, status in results:
        mark = "✅" if status == IDENTICAL else "⚠️"
        print(f"{mark} {status:<16} {relative}")

    unresolved = [item for item in results if item[1] == MISSING_UPSTREAM]
    actionable = [item for item in results if item[1] in (DRIFT, MISSING_LOCAL)]

    if args.sync and actionable:
        copied = sync(upstream_root, actionable)
        print(f"✅ re-synced {len(copied)} asset(s) from {upstream_root}")
        print("   review the diff and commit them — this fork ships them verbatim.")
        actionable = []

    if unresolved:
        print(
            f"❌ {len(unresolved)} asset(s) absent from {upstream_root} — the upstream "
            "brand set changed shape; reconcile BRAND_ASSETS with it by hand.",
            file=sys.stderr,
        )
        return 1
    if actionable:
        print(
            f"⚠️ {len(actionable)} asset(s) differ from upstream — re-run with --sync "
            "to copy them over.",
            file=sys.stderr,
        )
        return 1

    print(f"✅ all {len(results)} brand assets match {upstream_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
