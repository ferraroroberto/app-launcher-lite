"""The last publish per project, kept in the runtime data root and never in either repo.

``{project: {personal_sha, team_sha, published_at, pr_url}}``. ``personal_sha``
is where the next PR text starts; ``team_sha`` is the team main the publish
left behind, so a later team commit on top of it is a team-side change. An
unreadable file reads as empty: the next publish then treats every team commit
as unknown and asks for the second confirmation, which is the safe side.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from src import runtime_files
from src._json_io import read_json_or
from src.runtime_data import runtime_data_dir

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def state_file() -> Path:
    return runtime_data_dir("app-launcher-lite") / "team-mirror-state.json"


def _read(path: Path) -> Dict[str, Any]:
    return read_json_or(path, {}, "team mirror state", expect=dict)


def last_publish(project: str, path: Optional[Path] = None) -> Dict[str, Any]:
    row = _read(path or state_file()).get(project)
    return row if isinstance(row, dict) else {}


def record_publish(project: str, row: Dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or state_file()
    with _LOCK:
        data = _read(target)
        data[project] = row
        runtime_files.make_dirs(target.parent)
        tmp = target.with_suffix(".tmp")
        runtime_files.save_text(tmp, json.dumps(data, indent=2, sort_keys=True))
        runtime_files.replace(tmp, target)
    logger.info("✅ team mirror: recorded the publish of %s", project)
