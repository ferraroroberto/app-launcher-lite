"""Settings > Performance: every speed limit the owner can tune, in one table.

A slower computer needs other poll spacings, pool sizes and cache lifetimes
than a fast one, so each limit is a :class:`~src.webapp_config.WebappConfig`
field with a default that suits an ordinary machine, a hard range, and a value
in the **Light machine** preset. :data:`KNOBS` is the single declaration: the
dataclass defaults, the validation, the presets and the Settings sheet all
read it, so a limit added here shows up everywhere at once.

The two presets are whole value sets. ``standard`` is every default; ``light``
spaces polls out, shrinks the pools and stretches the caches. A stored set that
matches neither is ``custom``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

PRESET_STANDARD = "standard"
PRESET_LIGHT = "light"
PRESET_CUSTOM = "custom"

KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"

GROUP_POLLS = "polls"
GROUP_POOLS = "pools"
GROUP_CACHES = "caches"


@dataclass(frozen=True)
class Knob:
    """One tunable limit: its config key, its default, its hard range and its Light value."""

    key: str
    label: str
    group: str
    kind: str
    default: Any
    light: Any
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str = ""
    # A pool size where 0 means "pick from the CPU count"; the range then
    # holds for every other value.
    auto_zero: bool = False
    # What the number controls and what too low or too high does, shown under the field.
    help: str = ""


KNOBS: Tuple[Knob, ...] = (
    Knob(
        "poll_board_s", "Board refresh", GROUP_POLLS, KIND_INT, 5, 15, 2, 120, "s",
        help="How often the Board asks for its columns while it is open. Slower spares a busy PC; "
             "faster shows a change sooner.",
    ),
    Knob(
        "poll_sessions_s", "Session list refresh", GROUP_POLLS, KIND_INT, 5, 15, 2, 120, "s",
        help="How often the list of running sessions refreshes.",
    ),
    Knob(
        "poll_transcript_s", "Open chat refresh", GROUP_POLLS, KIND_INT, 3, 8, 1, 60, "s",
        help="How often an open chat asks for what the agent has added.",
    ),
    Knob(
        "poll_pause_hidden", "Pause while the tab is hidden", GROUP_POLLS, KIND_BOOL, True, True,
        help="A hidden tab or a locked phone asks for nothing and reads once on return. "
             "Turn it off only if a hidden tab must keep the page current.",
    ),
    Knob(
        "pool_hot_poll", "Session and Board reads", GROUP_POOLS, KIND_INT, 8, 4, 4, 32, "threads",
        help="Threads that build the session list and the Board. The Board builds four steps at once, "
             "so fewer than four would serialise it.",
    ),
    Knob(
        "pool_session_reads", "Transcript reads", GROUP_POOLS, KIND_INT, 12, 6, 4, 48, "threads",
        help="Threads that read open chats. A stalled read holds one, so a smaller pool queues "
             "other chats sooner.",
    ),
    Knob(
        "pool_sends", "Sends and other requests", GROUP_POOLS, KIND_INT, 0, 8, 4, 64, "threads",
        auto_zero=True,
        help="Threads behind sending to a session and every route without its own pool. "
             "0 picks from the CPU count (CPUs + 4, at most 32).",
    ),
    Knob(
        "pool_git_status", "Git status", GROUP_POOLS, KIND_INT, 2, 1, 1, 32, "threads",
        help="Repos scanned in parallel, which is also the number of git processes running at once "
             "during a scan. Fewer is gentler on a slow PC and takes longer for a big fleet.",
    ),
    Knob(
        "git_status_ttl_s", "Git status cache", GROUP_CACHES, KIND_INT, 10, 30, 2, 60, "s",
        help="How long a scan is served as it is. Past this the next poll shows it at once "
             "and refreshes it behind the response.",
    ),
    Knob(
        "transcript_page_cache_size", "Transcript page cache", GROUP_CACHES, KIND_INT, 32, 16, 0, 256, "pages",
        help="Parsed chat pages kept for files that have not changed. 0 turns the cache off.",
    ),
    Knob(
        "transcript_tail_cache_mb", "Transcript tail cache", GROUP_CACHES, KIND_INT, 32, 16, 0, 256, "MB",
        help="Session transcript tails kept for files that have not changed, so the session list and "
             "the Board read each transcript once per change. 0 turns the cache off.",
    ),
    Knob(
        "transcript_resolve_miss_ttl_s", "Missing-log wait", GROUP_CACHES, KIND_FLOAT, 10.0, 30.0, 1, 120, "s",
        help="How long a Copilot session whose event log was not found answers not found "
             "before the session-state folder is scanned again.",
    ),
    Knob(
        "request_budget_scale", "Request budgets", GROUP_CACHES, KIND_FLOAT, 1.0, 2.0, 0.5, 10, "x",
        help="Multiplies every request class's soft and hard time budget, so a slower machine does "
             "not log a breach for every ordinary request. Per-class values stay in request_budgets.",
    ),
)

KEYS: Tuple[str, ...] = tuple(k.key for k in KNOBS)
_BY_KEY: Dict[str, Knob] = {k.key: k for k in KNOBS}

#: The two presets as whole value sets, keyed by config field.
STANDARD: Dict[str, Any] = {k.key: k.default for k in KNOBS}
LIGHT: Dict[str, Any] = {k.key: k.light for k in KNOBS}
PRESETS: Dict[str, Dict[str, Any]] = {PRESET_STANDARD: STANDARD, PRESET_LIGHT: LIGHT}


def default(key: str) -> Any:
    """The shipped default of ``key`` (the dataclass field's default)."""
    return _BY_KEY[key].default


def maximum(key: str) -> int:
    """The hard upper limit of a whole-number limit (a pool's thread ceiling)."""
    return int(_BY_KEY[key].maximum)


def _number_word(knob: Knob) -> str:
    return "a whole number" if knob.kind == KIND_INT else "a number"


def _range_text(knob: Knob) -> str:
    low, high = _fmt(knob.minimum), _fmt(knob.maximum)
    unit = f" {knob.unit}" if knob.unit else ""
    zero = " (or 0 for automatic)" if knob.auto_zero else ""
    return f"between {low} and {high}{unit}{zero}"


def _fmt(number: Optional[float]) -> str:
    if number is None:
        return "?"
    return str(int(number)) if float(number).is_integer() else f"{number:g}"


def type_problem(key: str, value: Any) -> Optional[str]:
    """Why ``value`` is the wrong type for ``key``, or ``None``.

    A bool is an int in Python and would pass a numeric check silently, and a
    float would be truncated by an integer field, so both are refused by name.
    """
    knob = _BY_KEY[key]
    if knob.kind == KIND_BOOL:
        return None if isinstance(value, bool) else f"{knob.label} must be on or off."
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{knob.label} must be {_number_word(knob)} {_range_text(knob)}."
    if knob.kind == KIND_INT and not float(value).is_integer():
        return f"{knob.label} must be a whole number {_range_text(knob)}; got {value:g}."
    return None


def range_problem(knob: Knob, value: Any) -> Optional[str]:
    """Why ``value`` is outside the hard range of ``knob``, or ``None``."""
    if knob.kind == KIND_BOOL:
        return None
    if knob.auto_zero and value == 0:
        return None
    if knob.minimum <= value <= knob.maximum:
        return None
    return f"{knob.label} must be {_number_word(knob)} {_range_text(knob)}; got {_fmt(value)}."


def problems(values: Mapping[str, Any]) -> List[str]:
    """One sentence per limit in ``values`` that is the wrong type or out of range."""
    out: List[str] = []
    for key, value in values.items():
        knob = _BY_KEY.get(key)
        if knob is None:
            continue
        message = type_problem(key, value) or range_problem(knob, value)
        if message:
            out.append(message)
    return out


def values_of(cfg: Any) -> Dict[str, Any]:
    """The current value of every limit, read off a config object."""
    return {key: getattr(cfg, key) for key in KEYS}


def repair(cfg: Any) -> List[str]:
    """Put back the default of each limit a hand-edited file holds out of range.

    A file with one bad number must not stop the webapp starting, so a loaded
    config is repaired and a line says which value was replaced; a *save* is
    refused instead (``problems``), because there the owner can correct it.
    """
    replaced: List[str] = []
    for knob in KNOBS:
        value = getattr(cfg, knob.key)
        message = type_problem(knob.key, value) or range_problem(knob, value)
        if message:
            setattr(cfg, knob.key, knob.default)
            replaced.append(f"{knob.key} reset to its default {knob.default!r}: {message}")
    for line in replaced:
        logger.warning(f"⚠️  {line}")
    return replaced


def detect_preset(values: Mapping[str, Any]) -> str:
    """``standard``, ``light`` or ``custom``: which preset ``values`` equals."""
    for name, preset in PRESETS.items():
        if all(values.get(key) == preset[key] for key in KEYS):
            return name
    return PRESET_CUSTOM


def payload(cfg: Any) -> Dict[str, Any]:
    """What ``GET /api/config`` carries for the Performance sheet: values, ranges and presets."""
    values = values_of(cfg)
    return {
        "preset": detect_preset(values),
        "values": values,
        "presets": {name: dict(preset) for name, preset in PRESETS.items()},
        "knobs": [
            {
                "key": k.key, "label": k.label, "group": k.group, "kind": k.kind,
                "default": k.default, "light": k.light, "min": k.minimum, "max": k.maximum,
                "unit": k.unit, "auto_zero": k.auto_zero, "help": k.help,
            }
            for k in KNOBS
        ],
    }
