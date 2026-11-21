"""Writers for runtime files whose path the runtime-write ratchet cannot resolve.

A path that is a parameter or a CLI argument (``--out``, ``out_dir``, ``log_path``)
cannot be followed statically, so each such write would need its own exempt marker.
These writers take the path, refuse it at runtime when it resolves, links and junctions
followed, under one of the code folders the ratchet guards, and then write. The guarantee
a marker only claimed ("never beside the code") is enforced on every call.

``hooks/state`` is the one in-tree exception: ``~/.claude/hooks`` is a junction into this
repo, so the live hooks-state folder resolves under ``hooks/``. It is gitignored runtime
state, not code.

The ``hooks/runtime_files`` package carries the subset the hooks use: the two import
worlds cannot import each other (see ``no_window.py``), and
``tests/repo/test_runtime_files.py`` holds the shared functions to the same code.

stdlib only.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import IO, Any, Union

PathLike = Union[str, "os.PathLike[str]"]

#: `[runtime_write_ratchet] code_folders` in `.fleet.toml`; a test holds the two equal.
CODE_FOLDERS = ("hooks", "skills", "scripts", ".claude")

#: Gitignored runtime state that lives under a code folder (see the module docstring).
LIVE_STATE_FOLDERS = ("hooks/state",)


#: The checkout this copy lives in; a copied tree guards its own code folders.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _inside(path: Path, folder: Path) -> bool:
    child, parent = os.path.normcase(str(path)), os.path.normcase(str(folder))
    return child == parent or child.startswith(parent + os.sep)


def checked(path: PathLike) -> Path:
    """``path`` resolved, or ``ValueError`` when it sits under a code folder."""
    resolved = Path(path).expanduser().resolve()
    if any(_inside(resolved, REPO_ROOT / live) for live in LIVE_STATE_FOLDERS):
        return resolved
    for name in CODE_FOLDERS:
        if _inside(resolved, REPO_ROOT / name):
            raise ValueError(
                f"Refusing to write under the code folder {REPO_ROOT / name}: {resolved}. "
                "Runtime output belongs in the hooks state dir, a temp folder or the caller's own folder."
            )
    return resolved


def make_dirs(path: PathLike, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> Path:
    """``Path.mkdir`` on ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named folder; checked() above raises when it is under a code folder
    target.mkdir(mode, parents, exist_ok)
    return target


def touch_file(path: PathLike, mode: int = 0o666, exist_ok: bool = True) -> Path:
    """``Path.touch`` on ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named file; checked() above raises when it is under a code folder
    target.touch(mode, exist_ok)
    return target


def open_for_write(path: PathLike, mode: str = "w", *args: Any, **kwargs: Any) -> IO[Any]:
    """``open(path, mode, ...)`` for a writing ``mode``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named file; checked() above raises when it is under a code folder
    return open(target, mode, *args, **kwargs)


def save_text(path: PathLike, data: str, encoding: str | None = None, errors: str | None = None,
              newline: str | None = None) -> int:
    """``Path.write_text`` on ``path``; refuses a path under a code folder."""
    with open_for_write(path, "w", encoding=encoding, errors=errors, newline=newline) as handle:
        return handle.write(data)


def save_bytes(path: PathLike, data: bytes) -> int:
    """``Path.write_bytes`` on ``path``; refuses a path under a code folder."""
    with open_for_write(path, "wb") as handle:
        return handle.write(data)


def copy_file(src: PathLike, dst: PathLike, **kwargs: Any) -> Any:
    """``shutil.copyfile`` to ``dst``; refuses a destination under a code folder."""
    target = checked(dst)
    # runtime-write-exempt: caller-named destination; checked() above raises when it is under a code folder
    return shutil.copyfile(src, target, **kwargs)


def copy_with_metadata(src: PathLike, dst: PathLike, **kwargs: Any) -> Any:
    """``shutil.copy2`` to ``dst``; refuses a destination under a code folder."""
    target = checked(dst)
    # runtime-write-exempt: caller-named destination; checked() above raises when it is under a code folder
    return shutil.copy2(src, target, **kwargs)


def copy_tree(src: PathLike, dst: PathLike, **kwargs: Any) -> Any:
    """``shutil.copytree`` to ``dst``; refuses a destination under a code folder."""
    target = checked(dst)
    # runtime-write-exempt: caller-named destination; checked() above raises when it is under a code folder
    return shutil.copytree(src, target, **kwargs)


def move(src: PathLike, dst: PathLike) -> Any:
    """``shutil.move`` to ``dst``; refuses a destination under a code folder."""
    target = checked(dst)
    # runtime-write-exempt: caller-named destination; checked() above raises when it is under a code folder
    return shutil.move(str(src), str(target))


def file_handler(path: PathLike, mode: str = "a", encoding: str | None = None) -> logging.FileHandler:
    """A ``logging.FileHandler`` on ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named log file; checked() above raises when it is under a code folder
    return logging.FileHandler(target, mode=mode, encoding=encoding)


def connect_sqlite(path: PathLike, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    """``sqlite3.connect`` to the file ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named database; checked() above raises when it is under a code folder
    return sqlite3.connect(target, *args, **kwargs)


def connect_read_only(uri: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    """``sqlite3.connect`` to a ``file:`` URI that carries ``mode=ro``; any other URI is refused."""
    if "mode=ro" not in uri.partition("?")[2].split("&"):
        raise ValueError(f"Refusing a SQLite URI that is not mode=ro: {uri}")
    kwargs["uri"] = True
    # runtime-write-exempt: a read-only connection (mode=ro); nothing is written
    return sqlite3.connect(uri, *args, **kwargs)
