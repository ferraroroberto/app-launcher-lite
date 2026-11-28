"""The privacy scan over a snapshot, with the personal-data ratchet's own rules.

The rules and the machine-local term list are the vendored
``scripts/personal_data_ratchet.py``: user paths, personal email addresses,
the owner's handle as a value, and every term in the list
(``PERSONAL_DATA_TERMS``, else ``~/.config/personal-data/terms.toml``). The
webapp config's ``mirror_scan_terms`` adds words on top. A rule with nothing
to read makes the result ``unknown``, which blocks a publish exactly like a
match: an unread list is never a pass. Findings carry file, line and rule,
never the matched text.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_RATCHET = Path(__file__).resolve().parents[2] / "scripts" / "personal_data_ratchet.py"
_MODULE_NAME = "personal_data_ratchet"
EXTRA_RULE = "term-mirror"
# A shorter word matches ordinary text everywhere; the ratchet's own minimum.
MIN_TERM = 3

CLEAR, BLOCKED, UNKNOWN = "clear", "blocked", "unknown"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


@dataclass
class ScanResult:
    state: str = CLEAR
    findings: List[Finding] = field(default_factory=list)
    unknown: Dict[str, str] = field(default_factory=dict)  # rule -> why it could not run
    scanned: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "state": self.state,
            "findings": [{"path": f.path, "line": f.line, "rule": f.rule} for f in self.findings],
            "unknown": dict(self.unknown),
            "scanned": self.scanned,
        }


def _ratchet() -> ModuleType:
    """The vendored ratchet, loaded by path once (``scripts/`` is not a package)."""
    loaded = sys.modules.get(_MODULE_NAME)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _RATCHET)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_RATCHET.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def clean_terms(raw: object) -> Tuple[str, ...]:
    """The config's extra scan words: strings of at least :data:`MIN_TERM` characters, deduplicated."""
    words: List[str] = []
    for item in raw if isinstance(raw, list) else []:
        word = str(item or "").strip()
        if len(word) >= MIN_TERM and word.lower() not in (w.lower() for w in words):
            words.append(word)
    return tuple(words)


def load_terms(personal_dir: Path, extra: Sequence[str]) -> Tuple[Optional[object], Dict[str, str]]:
    """The ratchet's terms for ``personal_dir`` (its GitHub owner is the handle) plus ``extra``.

    Returns ``(terms, unknown)``; ``terms`` is None when the list itself could
    not be read, and ``unknown`` names every rule left with nothing to match.
    """
    try:
        ratchet = _ratchet()
    except (ImportError, OSError, SyntaxError) as exc:
        logger.warning("⚠️ team mirror: the personal-data scanner could not load (%s)", type(exc).__name__)
        return None, {"scanner": "the personal-data scanner could not be loaded"}
    try:
        terms = ratchet.load_terms(personal_dir)
    except (ratchet.ConfigError, ratchet.NotConfirmed) as exc:
        logger.warning("⚠️ team mirror: the term list is unusable (%s)", type(exc).__name__)
        return None, {"term-list": f"the term list could not be used: {exc}"}
    if extra:
        patterns = {**terms.patterns, EXTRA_RULE: ratchet._term_pattern(tuple(extra))}
        terms = ratchet.Terms(patterns, terms.handle, dict(terms.unknown))
    return terms, dict(terms.unknown)


def _decoded(data: bytes) -> Optional[str]:
    """Text of a file the scan reads; None for binary content (a NUL byte), as the ratchet skips it."""
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan(
    root: Path,
    paths: Iterable[str],
    personal_dir: Path,
    extra_terms: Sequence[str],
    texts: Sequence[Tuple[str, str]] = (),
) -> ScanResult:
    """Scan ``paths`` under ``root`` plus each ``(label, text)`` in ``texts`` (the PR text)."""
    terms, unknown = load_terms(personal_dir, extra_terms)
    result = ScanResult(unknown=unknown)
    if terms is None:
        result.state = UNKNOWN
        return result
    ratchet = _ratchet()

    def scan_text(text: str, path: str) -> None:
        try:
            found = ratchet.scan_source(text, path, terms)
        except ratchet.NotConfirmed as exc:  # a Python file that does not parse
            result.unknown[path] = f"could not be read through ({type(exc).__name__})"
            return
        result.findings.extend(Finding(path, line, rule) for line, rule in found.findings)
        result.scanned += 1

    for path in paths:
        try:
            data = (root / path).read_bytes()
        except OSError as exc:
            result.unknown[path] = f"could not be read ({type(exc).__name__})"
            continue
        text = _decoded(data)
        if text is not None:
            scan_text(text, path)
    for label, text in texts:
        scan_text(text, label)
    result.findings.sort(key=lambda f: (f.path, f.line, f.rule))
    if result.findings:
        result.state = BLOCKED
    elif result.unknown:
        result.state = UNKNOWN
    return result
