"""Shared file-log-handler bootstrap for the webapp's side logs (issue #11).

The webapp runs under ``--log-level warning`` and the tray spawns it with
stdout/stderr DEVNULL'd, so anything meant to be readable after the fact needs
its own ``FileHandler``. Attaching one is a fixed five-step contract — don't
double-attach across an app re-create, make the parent dir, build the handler,
set the level on *both* handler and logger, degrade to a warning when the file
can't be opened — and it was re-typed near-verbatim in
``app/webapp/routers/auth.py`` (``webapp/auth.log``) and
``app/webapp/observability.py`` (``webapp/slow-requests.log``), differing only
in the logger, the path and the level. Those three are the parameters here.

``src/audit.py``'s handler bootstrap deliberately stays on its own: it uses a
different line format and gates on a module-level ``_handler_ready`` flag
rather than the handler scan, so folding it in would mean parameterising this
helper for a single extra caller.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Shared line format for the webapp's side logs. Level is spelled out because
#: these files are read on a phone, out of context, long after the fact.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def ensure_file_log_handler(
    target: logging.Logger, path: Path, level: int
) -> None:
    """Attach a ``FileHandler`` writing ``path`` to ``target``, exactly once.

    Idempotent — safe to call from ``create_app()`` on every boot: a handler
    already pointed at the same resolved path short-circuits, so a re-created
    app never stacks duplicate handlers (and duplicate lines) on a logger that
    outlives it.

    Never raises. An unopenable path degrades to one warning and leaves
    ``target`` without a file handler — a side log is a breadcrumb, not a
    startup dependency.
    """
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == path.resolve()
        for h in target.handlers
    ):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        target.addHandler(handler)
        target.setLevel(level)
    except OSError as exc:
        logger.warning(f"⚠️  Could not open {path}: {exc}")
