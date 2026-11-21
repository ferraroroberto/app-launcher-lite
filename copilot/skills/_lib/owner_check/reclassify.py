"""Re-classify the open `needs-validation` list under the current rule.

A block records why an issue was put on the owner's list. This module re-runs the
rule over each such issue and keeps it only while a reason still fires. A lane's
named gap (a criterion plus an owner-only class) and any text a person wrote by
hand are never re-derived, so they keep the item, unless the narrowed classes
now refuse that gap as lane-provable (or its class is gone). A generic "could not verify" or
"did not state" names nothing the owner can do, so it drops. Everything else is
recomputed from the issue and its PR, with no in-session proof assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from owner_check import backfill, rule

KEEP = "keep"
CLEAR = "clear"
UNKNOWN = "unknown"

# Recorded reasons the rule produces from the issue or the diff, by their opening words.
_RULE_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("the issue says Owner validation", "explicit"),
    ("needs a real device or live input", "device"),
    ("changes a live-input path", "device"),
    ("involves an account only the owner holds", "own-sign-in"),
    ("involves a third-party account", "account"),
    ("adds a new user-facing surface", "surface"),
    ("adds a new page or view", "surface"),
)
# Generic verification reasons from before a gap had to name its criterion and class.
_GENERIC_PREFIXES = ("the lane did not state", "the lane could not verify")

# One public-safe phrase per reason that stopped firing.
DROPPED_WORDS = {
    "own-sign-in": "an app's own sign-in is not a third-party account",
    "surface": "a new page or view is browser-provable, so an agent proves it, not the owner",
    "backfill": "back-fill-only entry",
    "device": "no live-input reason any more",
    "generic": "a generic could-not-verify names no criterion and no owner-only class",
    "explicit": "the issue no longer says Owner validation: required",
    "account": "no third-party account involved",
    "lane-provable": "the named gap is lane-provable under the narrowed owner classes",
}


@dataclass
class Verdict:
    state: str
    kept: List[str]
    dropped: List[str]
    why: str = ""


def _pr_number(proof: Sequence[str]) -> Optional[int]:
    for link in proof:
        m = re.search(r"/pull/(\d+)", link)
        if m:
            return int(m.group(1))
    return None


def _split(reason: str) -> List[str]:
    """Recorded reasons are joined with `; `, but a gap's criterion may hold one too.

    A gap opens with `GAP_PREFIX` and closes at its `because it needs <class>` clause,
    so pieces are re-joined until `recorded_gap` reads the whole gap back. A gap that
    never closes stays split as written.
    """
    pieces = [part.strip() for part in reason.split("; ") if part.strip()]
    parts: List[str] = []
    i = 0
    while i < len(pieces):
        end = i
        if pieces[i].startswith(rule.GAP_PREFIX):
            for j in range(i, len(pieces)):
                if rule.recorded_gap("; ".join(pieces[i:j + 1])):
                    end = j
                    break
        parts.append("; ".join(pieces[i:end + 1]))
        i = end + 1
    return parts


def classify_recorded(part: str) -> str:
    """`rule:<key>`, `lane`, `lane-provable`, `generic`, `backfill` or `hand` for one recorded reason."""
    if part == backfill.BACKFILL_REASON:
        return "backfill"
    for prefix, key in _RULE_PREFIXES:
        if part.startswith(prefix):
            return f"rule:{key}"
    if part.startswith(rule.GAP_PREFIX):
        gap = rule.recorded_gap(part)
        if gap and (gap[0] not in rule.OWNER_CLASSES or rule.gap_refusal(*gap)):
            return "lane-provable"
        return "lane"
    if part.startswith(_GENERIC_PREFIXES):
        return "generic"
    return "hand"


def reclassify(
    item: dict,
    issue: dict,
    pr_files: Callable[[int], List[Tuple[str, str]]],
) -> Verdict:
    """Keep or clear one `validate-after` item. `issue` is `github.fetch_issue`'s dict."""
    parts = _split(item.get("reason") or "")
    if not item.get("block") or not parts:
        return Verdict(UNKNOWN, [], [], "no recorded reason to re-check")
    recorded = [(part, classify_recorded(part)) for part in parts]
    kept = [part for part, kind in recorded if kind in ("lane", "hand")]
    pr = _pr_number(item.get("proof") or [])
    fired: List[str] = []
    if pr is None:
        if any(kind.startswith("rule:") for _part, kind in recorded):
            return Verdict(UNKNOWN, kept, [], "no PR link to re-read the diff")
    else:
        # No proof is inferred from history, so a recorded device reason never drops here.
        decision = rule.decide(issue["labels"], issue["title"], issue["body"], pr_files(pr), rule.VERIFIED_NA)
        fired = list(decision.reasons)
    now = kept + fired
    still = {classify_recorded(r).split(":", 1)[-1] for r in fired}
    dropped = sorted({kind.split(":", 1)[-1] for _part, kind in recorded
                      if (kind in ("backfill", "generic", "lane-provable") or kind.startswith("rule:"))
                      and kind.split(":", 1)[-1] not in still})
    return Verdict(KEEP if now else CLEAR, now, dropped)
