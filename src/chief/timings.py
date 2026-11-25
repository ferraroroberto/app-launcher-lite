"""The chief's timings: the ones Settings > Chief may tune, and the fixed ones it shows.

:data:`TUNABLES` is the single source for each tunable's default and range:
``WebappConfig`` takes its defaults from it, ``_validate`` its ranges, and
``GET /api/config`` / ``GET /api/board/chief/settings`` both. Every default equals
the constant it replaced, so an install that never edits a field behaves as before.

:data:`FIXED` is what the read-only "How the chief works" panel lists. Its values
are copies of constants that live in modules this one may not import (the
doorbell under ``app/``, the hooks and ``chief_ops`` under ``copilot/``);
``tests/unit/chief/test_chief_timings.py`` pins each copy to its source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class Tunable:
    key: str
    default: int
    minimum: int
    maximum: int
    unit: str  # "seconds" | "minutes"


TUNABLES: Tuple[Tunable, ...] = (
    # Doorbell back-off: a chief still idle with mail pending this long after a
    # ring never got it, so the bell rings again.
    Tunable("chief_rering_seconds", 60, 30, 600, "seconds"),
    # A pending event this old means the chief missed its wake: one warning in
    # the doorbell log and the chief card moves to Your turn.
    Tunable("chief_unread_alert_minutes", 15, 5, 240, "minutes"),
    # An unacked mailbox claim this old is offered again. The floor stays above
    # any chief turn that handles a batch, so a live chief never gets its own
    # batch twice.
    Tunable("chief_reoffer_minutes", 15, 10, 120, "minutes"),
    # The turn-end hook holds the chief at most once per window, which is what
    # keeps a hold from looping.
    Tunable("chief_hold_minutes", 10, 5, 60, "minutes"),
)

TUNABLE_KEYS: Tuple[str, ...] = tuple(t.key for t in TUNABLES)
_BY_KEY: Dict[str, Tunable] = {t.key: t for t in TUNABLES}


def default(key: str) -> int:
    return _BY_KEY[key].default


def range_problem(key: str, value: Any) -> str:
    """Why ``value`` is not a valid ``key``, or ``""`` when it is."""
    spec = _BY_KEY[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{key} must be a whole number of {spec.unit}; got {value!r}"
    if not spec.minimum <= value <= spec.maximum:
        return (f"{key} must be between {spec.minimum} and {spec.maximum} "
                f"{spec.unit}; got {value}")
    return ""


def config_payload(cfg: object) -> Dict[str, Any]:
    """Each tunable's value, with ``<key>_min`` / ``<key>_max`` for the field."""
    body: Dict[str, Any] = {}
    for spec in TUNABLES:
        body[spec.key] = getattr(cfg, spec.key, spec.default)
        body[f"{spec.key}_min"] = spec.minimum
        body[f"{spec.key}_max"] = spec.maximum
    return body


def values(cfg: object) -> Dict[str, int]:
    return {spec.key: getattr(cfg, spec.key, spec.default) for spec in TUNABLES}


@dataclass(frozen=True)
class Fixed:
    label: str
    value: str
    why: str


FIXED: Tuple[Fixed, ...] = (
    Fixed("Doorbell check", "every 3 s",
          "How often the launcher looks for an idle chief with mail waiting."),
    Fixed("Ring settle time", "3 s",
          "The chief must stay idle this long first, so lanes that finish together make one ring."),
    Fixed("Stuck 'working' label", "10 min silent",
          "A chief marked working whose terminal printed nothing this long counts as idle."),
    Fixed("Fallback check", "every 60 s",
          "How often the fallback wake looks at the mailbox and free worker slots."),
    Fixed("Missed turn-end recovery", "15 s check, 30 s grace",
          "A worker's turn end that was missed still reaches the chief within about 45 s."),
    Fixed("Turn-end repeat", "45 s",
          "A second turn end from the same worker inside this window is not mailed again."),
    Fixed("Mail expiry", "24 h",
          "Mail older than this describes work the chief can no longer act on, so it is dropped."),
    Fixed("Compact freshness", "10 min",
          "The chief compacts only if its handover log and plan were written this recently."),
    Fixed("Idle-slot reading", "2 min",
          "The turn-end hold trusts the last count of free worker slots only this long."),
    Fixed("New lane grace", "10 min",
          "A fresh lane reads as finished until it commits, so the sweep leaves it alone this long."),
    Fixed("Board 'stalled'", "30 min",
          "A background dispatch still pending after this long shows as stalled on the Board."),
    Fixed("Respawn wait", "8 s",
          "Respawn waits this long for the old chief to quit before stopping it, so there are never two."),
    Fixed("Resume window", "7 days",
          "Resume offers the last chief conversation only if it is this recent."),
)

#: Rules that are not times.
FIXED_RULES: Tuple[str, ...] = (
    "The doorbell types only into a chief that is idle at its prompt with nothing typed; "
    "an unknown composer never rings.",
    "The line it types carries no data: the chief reads everything from its mailbox.",
    "The chief itself never counts against the worker cap.",
)


def fixed_payload() -> Dict[str, List[Any]]:
    return {
        "values": [{"label": f.label, "value": f.value, "why": f.why} for f in FIXED],
        "rules": list(FIXED_RULES),
    }
