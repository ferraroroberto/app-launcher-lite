"""The folder that holds the repos the chief works on: app-launcher-lite's own `projects_dir`.

The chief dispatches lanes into `<root>/<repo>` and sweeps `<root>/<repo>-wt-<N>`.
app-launcher-lite already has a setting for that folder (`projects_dir` in its
gitignored `config/webapp_config.json`), so the chief reads it from there instead
of counting directory levels up from its own file. An empty or missing setting, or
one that names no folder, falls back to the folder this checkout sits in, which is
what app-launcher-lite's own default does.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# copilot/skills/_lib/ -> the app-launcher-lite checkout root, three levels up.
LITE_ROOT = Path(__file__).resolve().parents[3]
WEBAPP_CONFIG = LITE_ROOT / "config" / "webapp_config.json"


def _configured(path: Path) -> str:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ could not read %s (%s); using the folder beside this checkout", path, exc)
        return ""
    value = raw.get("projects_dir") if isinstance(raw, dict) else None
    return value.strip() if isinstance(value, str) else ""


def repos_root(config: Optional[Path] = None, lite_root: Optional[Path] = None) -> Path:
    """`projects_dir` when it names a folder, else the parent of this checkout."""
    configured = _configured(config or WEBAPP_CONFIG)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        logger.warning("⚠️ projects_dir %r is not a folder; using the folder beside this checkout", configured)
    return (lite_root or LITE_ROOT).parent
