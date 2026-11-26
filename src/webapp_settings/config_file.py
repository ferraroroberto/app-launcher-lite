"""Reading ``webapp_config.json``, and refusing to write over one that cannot be read.

The loader answers an unreadable file with defaults, so a save built on that
answer would replace the owner's values with them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConfigUnreadableError(ValueError):
    """A save was refused because the file on disk cannot be read as settings."""


def read_json(target: Path) -> Any:
    """The file's JSON value."""
    # utf-8-sig: a Windows editor may save the file with a byte-order mark.
    return json.loads(target.read_text(encoding="utf-8-sig"))


def refuse_unreadable(target: Path) -> None:
    """Raise :class:`ConfigUnreadableError` when ``target`` holds no readable settings.

    A missing file is fine: there is nothing to lose.
    """
    if not target.exists():
        return
    try:
        problem = "" if isinstance(read_json(target), dict) else "it is not a JSON object"
    except OSError as exc:
        # strerror, not str(exc): the full path stays out of the HTTP answer.
        problem = exc.strerror or type(exc).__name__
    except ValueError as exc:
        problem = str(exc)
    if problem:
        logger.warning(f"⚠️  Refusing to save over {target}: {problem}")
        raise ConfigUnreadableError(
            f"{target.name} could not be read ({problem}), so nothing was saved. "
            "Fix the file or move it away, then save again."
        )
