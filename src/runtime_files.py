"""Writers for runtime files whose path the runtime-write ratchet cannot resolve.

The launcher's state lives where a parameter, a config value or a request says
(``webapp/``, ``config/``, a project folder the user picked). The ratchet cannot
follow such a path statically, so every write would need its own exempt marker.
These writers take the path, refuse it at runtime when it resolves under one of
the code folders the ratchet guards, and then write. The guarantee a marker only
claimed is enforced here.

Gitignored live state inside the checkout (``webapp/``, ``config/``) is not a
code folder, so the existing runtime paths keep working.

Stdlib-only and import-free of the rest of the repo, so the detached
session-host can load it too.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import IO, Any, Union

PathLike = Union[str, "os.PathLike[str]"]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The folders `[runtime_write_ratchet] code_folders` guards (its default set).
CODE_FOLDERS = ("src", "app", "scripts", "hooks", "skills")


def checked(path: PathLike) -> Path:
    """``path`` resolved, or ``ValueError`` when it sits under a code folder."""
    resolved = Path(path).expanduser().resolve()
    for name in CODE_FOLDERS:
        folder = REPO_ROOT / name
        if resolved == folder or folder in resolved.parents:
            raise ValueError(
                f"Refusing to write under the code folder {folder}: {resolved}. "
                "Runtime output belongs in webapp/, config/ or a project folder."
            )
    return resolved


def make_dirs(path: PathLike, *, parents: bool = True, exist_ok: bool = True) -> Path:
    """Create the folder ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named folder; checked() above raises when it is under a code folder
    target.mkdir(parents=parents, exist_ok=exist_ok)
    return target


def open_for_write(path: PathLike, mode: str = "w", **kwargs: Any) -> IO[Any]:
    """Open ``path`` with ``mode`` (creating no folders); refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named file; checked() above raises when it is under a code folder
    return target.open(mode, **kwargs)


def save_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "w", encoding=encoding) as handle:
        handle.write(text)


def save_bytes(path: PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "wb") as handle:
        handle.write(data)


def file_handler(path: PathLike, mode: str = "a", encoding: str = "utf-8") -> logging.FileHandler:
    """A ``logging.FileHandler`` on ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named log file; checked() above raises when it is under a code folder
    return logging.FileHandler(target, mode=mode, encoding=encoding)


def connect_sqlite(path: PathLike, **kwargs: Any) -> sqlite3.Connection:
    """Open the SQLite file ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named database; checked() above raises when it is under a code folder
    return sqlite3.connect(target, **kwargs)
