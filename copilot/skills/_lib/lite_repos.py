"""The folder that holds the repos the chief works on: app-launcher-lite's own `projects_dir`.

The chief dispatches lanes into `<root>/<repo>` and sweeps `<root>/<repo>-wt-<N>`.
app-launcher-lite already has a setting for that folder (`projects_dir` in its
gitignored `config/webapp_config.json`), so the chief reads it from there instead
of counting directory levels up from its own file. An empty or missing setting, or
one that names no folder, falls back to the folder this checkout sits in, which is
what app-launcher-lite's own default does.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import lite_config
from lite_config import LITE_ROOT

logger = logging.getLogger(__name__)


def repos_root(config: Optional[Path] = None, lite_root: Optional[Path] = None) -> Path:
    """`projects_dir` when it names a folder, else the parent of this checkout."""
    configured = lite_config.folder("projects_dir", config)
    if configured:
        if configured.is_dir():
            return configured.resolve()
        logger.warning("⚠️ projects_dir %s is not a folder; using the folder beside this checkout", configured)
    return (lite_root or LITE_ROOT).parent
