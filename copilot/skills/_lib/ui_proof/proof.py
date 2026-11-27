"""What state a captured proof is in, and how a layout-lint manifest and criteria are read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gate_evidence

FRESH, STALE, FAILING, MISSING, UNKNOWN, NOT_APPLICABLE = "fresh", "stale", "failing", "missing", "unknown", "n/a"


def tree_of(repo: Path) -> Tuple[Optional[str], str]:
    """`(tree hash of the working tree, "")`, or `(None, why)`. Ignored files, `.proof/` among them, stay out."""
    return gate_evidence.tree_hash(repo)


def load_manifest(path: Path) -> Optional[List[dict]]:
    """A layout-lint manifest's entries, or `None` when it is missing or not a manifest."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else None


def finding_fails(finding: dict) -> bool:
    return finding.get("severity") == "error" and not finding.get("allowed")


def failing_lines(rows: List[dict]) -> List[str]:
    """One line (leg, kind, element, measurement; never page text) per failing finding."""
    lines = []
    for row in rows:
        for f in row.get("findings") or []:
            if finding_fails(f):
                pair = f" x {f['other']}" if f.get("other") else ""
                lines.append(f"{row.get('state')} {row.get('viewport')}-{row.get('theme')}: "
                             f"{f.get('kind')} {f.get('element')}{pair}: {f.get('detail')}")
    return lines


def parse_criteria(pairs: List[str]) -> Dict[str, List[str]]:
    """`["state=criterion text", ...]` -> `{state: [criterion, ...]}`; raises ValueError on a malformed pair."""
    criteria: Dict[str, List[str]] = {}
    for pair in pairs:
        state, sep, text = pair.partition("=")
        if not sep or not state.strip() or not text.strip():
            raise ValueError(f"--criterion needs '<state>=<criterion text>', got {pair!r}")
        criteria.setdefault(state.strip(), []).append(text.strip())
    return criteria


def judge(repo: Path, data: dict) -> Tuple[str, str]:
    """`(state, why)` for captured data against the current tree. A tree that cannot be read is never `fresh`."""
    after = data.get("after") or []
    if not after:
        return MISSING, "no after capture"
    captured = data.get("after_tree")
    current, why = tree_of(repo)
    if current is None or not captured:
        return UNKNOWN, why or "the after capture recorded no tree hash"
    if current != captured:
        return STALE, "the tree changed since the after capture; capture it again"
    errors = failing_lines(after)
    if errors:
        return FAILING, "layout lint errors: " + "; ".join(errors)
    return FRESH, ""
