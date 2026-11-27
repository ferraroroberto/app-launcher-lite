"""Reading fields out of a decoded request body or WebSocket frame.

Lives in this package because both the webapp's routes and the session-host
read the same launch and resize fields, and the session-host loads no webapp
router code.
"""

from __future__ import annotations

from typing import Any, Dict


def safe_int(body: Dict[str, Any], key: str, default: int) -> int:
    """An ``int``-ish field of a decoded request body.

    ``default`` when the field is absent, falsy or does not coerce: a malformed
    body (``{"rows": "abc"}``) must not raise, because an uncaught
    ``ValueError`` is a bare 500. ``OverflowError`` is the infinity a JSON
    number like ``1e999`` decodes to.
    """
    raw = body.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
