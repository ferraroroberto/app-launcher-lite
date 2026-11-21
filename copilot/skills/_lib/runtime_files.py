"""Guarded writers for files whose destination a caller names.

The skills write into a folder the operator passes on the command line (a deck
dir, an --out-dir, the repo an e2e bootstrap targets). The runtime-write ratchet
cannot follow such a path statically, so every write would need its own exempt
marker. These writers take the path, refuse it at runtime when it resolves under
one of the folders the ratchet guards, and only then write: the guarantee a marker
would only claim is enforced here.

Stdlib-only and import-free of the rest of the repo, so any skill can use it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Any, Union

PathLike = Union[str, "os.PathLike[str]"]

REPO_ROOT = Path(__file__).resolve().parents[2]

# The folders `[runtime_write_ratchet] code_folders` guards (its default set).
CODE_FOLDERS = ("src", "app", "scripts", "hooks", "skills")


def checked(path: PathLike) -> Path:
    """Return ``path`` resolved, or raise ``ValueError`` when it sits under a code folder."""
    resolved = Path(path).expanduser().resolve()
    for name in CODE_FOLDERS:
        folder = REPO_ROOT / name
        if resolved == folder or folder in resolved.parents:
            raise ValueError(
                f"refusing to write under the code folder {folder}: {resolved} "
                "(runtime output belongs outside the repo's code folders)"
            )
    return resolved


def make_dirs(path: PathLike) -> Path:
    """Create the folder ``path`` and its parents; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named folder; checked() above raises when it is under a code folder
    target.mkdir(parents=True, exist_ok=True)
    return target


def open_for_write(path: PathLike, mode: str = "w", **kwargs: Any) -> IO[Any]:
    """Open ``path`` for writing without creating folders; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named file; checked() above raises when it is under a code folder
    return target.open(mode, **kwargs)


def save_text(path: PathLike, text: str, encoding: str = "utf-8", **kwargs: Any) -> None:
    """Write ``text`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "w", encoding=encoding, **kwargs) as handle:
        handle.write(text)


def save_bytes(path: PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "wb") as handle:
        handle.write(data)
