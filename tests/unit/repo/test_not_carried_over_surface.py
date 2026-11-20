"""A feature CLAUDE.md lists as "not carried over" has no reachable surface.

A sync from app-launcher can bring a switch, a card or a route for a feature
this fork dropped, and the docs then contradict what a person can tap. These
patterns name the markers such a surface leaves in shipped markup and routes;
a hit fails the build, and a feature that returns on purpose comes off the
"Not carried over" list in CLAUDE.md and README.md in the same change.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_STATIC = _ROOT / "app" / "webapp" / "static"
_ROUTERS = _ROOT / "app" / "webapp" / "routers"

# feature -> (the feature's name as the "Not carried over" list spells it, a
# marker of its surface in shipped markup, script or route declarations).
_RETIRED = {
    "telegram-job-alert": ("Telegram", re.compile(r"alert[ -_]to[ -_]telegram|alert[-_]on[-_]failure", re.I)),
    "delegate-token": ("`/delegate`", re.compile(r"delegate[-_ ]?token", re.I)),
}


def _shipped_files() -> List[Path]:
    """Markup and script a person's browser loads, minus vendored bundles."""
    files = [
        path
        for pattern in ("*.html", "*.js")
        for path in _STATIC.rglob(pattern)
        if "vendor" not in path.parts and "_vendored" not in path.parts
    ]
    return sorted(files) + sorted(_ROUTERS.glob("*.py"))


def test_no_retired_feature_surface_ships() -> None:
    hits = [
        f"{path.relative_to(_ROOT).as_posix()}: {feature}"
        for path in _shipped_files()
        for feature, (_, marker) in _RETIRED.items()
        if marker.search(path.read_text(encoding="utf-8"))
    ]
    assert not hits, "a not-carried-over feature has a surface again:\n" + "\n".join(hits)


def test_every_retired_feature_is_still_listed_as_not_carried_over() -> None:
    """The table above is only a guard while the docs still say the feature is gone."""
    claude = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = claude.index("**Not carried over from app-launcher:**")
    listed = claude[start:claude.index("\n", start)]
    missing = [name for name, _ in _RETIRED.values() if name not in listed]
    assert not missing, f"CLAUDE.md no longer lists {missing}: drop the entry from _RETIRED too"
