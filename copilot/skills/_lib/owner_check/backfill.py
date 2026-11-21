"""Back-fill: put shipped-but-unvalidated work on the owner-check list once.

Two candidate sources, merged: an audit list of work items (`repo#N`, ranges and
`,#M` continuations, one item per `|` or `;` segment) and the rule's dry run over
enhancements closed in a date window. An audit item spanning several issues
becomes one Board entry: its lowest-numbered issue carries the label and block,
the rest are named in `related`. The block's text is generic on purpose, so a
private repo's personal content never reaches a comment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from owner_check import rule

ALIASES: Dict[str, str] = {
    "fs": "facilitation-suite",
    "cm": "content-management",
    "al": "app-launcher",
    "hub": "local-llm-hub",
    "lite": "app-launcher-lite",
    "grocery": "grocery-shopping-automation",
}

BACKFILL_REASON = "Shipped with no owner check on record"
BACKFILL_TRY = (
    "Use what this issue shipped once, as a user, the way the issue describes it; "
    "then answer on the Board: works, or what needs fixing."
)

_REF = re.compile(r"(?<![\w#-])([a-z][a-z0-9-]*)#(\d+)(?:-(\d+))?((?:,#\d+(?:-\d+)?)*)", re.IGNORECASE)
_MORE = re.compile(r"#(\d+)(?:-(\d+))?")


@dataclass
class Item:
    repo: str
    numbers: List[int]
    source: str
    reasons: List[str] = field(default_factory=list)

    @property
    def anchor(self) -> int:
        return min(self.numbers)

    @property
    def related(self) -> List[str]:
        return [f"#{n}" for n in sorted(set(self.numbers)) if n != self.anchor]


def _expand(start: str, end: Optional[str]) -> List[int]:
    lo = int(start)
    hi = int(end) if end else lo
    return list(range(lo, hi + 1)) if hi >= lo else [lo]


def parse_audit_list(text: str, aliases: Optional[Dict[str, str]] = None) -> List[Item]:
    """One Item per segment that names at least one `repo#N`; a repeated repo in one segment merges."""
    aliases = ALIASES if aliases is None else aliases
    items: List[Item] = []
    for line in text.splitlines():
        for segment in re.split(r"[|;]", line):
            by_repo: Dict[str, List[int]] = {}
            for m in _REF.finditer(segment):
                repo = aliases.get(m.group(1).lower(), m.group(1).lower())
                nums = _expand(m.group(2), m.group(3))
                for more in _MORE.finditer(m.group(4) or ""):
                    nums += _expand(more.group(1), more.group(2))
                by_repo.setdefault(repo, []).extend(nums)
            for repo, nums in by_repo.items():
                items.append(Item(repo, sorted(set(nums)), "audit"))
    return items


def rule_candidates(
    repo: str, closed: Iterable[dict], since: str, until: str,
) -> List[Item]:
    """Completed issues closed in `[since, until]` (dates, inclusive) that the rule flags."""
    found = []
    for row in closed:
        day = str(row.get("closedAt") or "")[:10]
        if not (since <= day <= until) or row.get("stateReason") not in (None, "COMPLETED"):
            continue
        decision = rule.decide(row.get("labels") or [], row.get("title") or "", row.get("body") or "")
        if decision.verdict == rule.REQUIRED:
            found.append(Item(repo, [int(row["number"])], "rule", list(decision.reasons)))
    return found


def merge(audit: Sequence[Item], ruled: Sequence[Item]) -> List[Item]:
    """Audit items first; a rule hit already inside an audit item is dropped."""
    covered: Set[Tuple[str, int]] = {(it.repo, n) for it in audit for n in it.numbers}
    out = list(audit)
    for it in ruled:
        if (it.repo, it.anchor) not in covered:
            covered.add((it.repo, it.anchor))
            out.append(it)
    return out


def resolve_anchor(item: Item, is_pr: Callable[[str, int], bool]) -> Item:
    """Drop leading PR numbers, so the label lands on the item's first issue.

    Only the anchor has to be an issue; a PR left in `related` still links.
    """
    numbers = sorted(set(item.numbers))
    while numbers and is_pr(item.repo, numbers[0]):
        numbers.pop(0)
    item.numbers = numbers
    return item
