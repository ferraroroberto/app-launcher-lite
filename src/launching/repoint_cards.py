"""Repoint app cards left in a suffixed sibling folder after a migration.

``setup.bat -MigrateFrom`` runs this once the migrated clone is verified (``-InPlace`` and
``-Replace`` reach it through the same call). A trial copy of the app sits beside it as
``<app>-new`` or ``<app>-e2e``, and a card in ``config/apps.json`` whose ``bat_path`` lies there
still starts that copy. Such a card's ``bat_path`` moves to the same file in this app's folder;
every other card and every other field is written back as it was. A card is left alone, and
said so, when this folder has no such file or another card already starts it.

Stdlib only: a ``-WhatIf`` run has no ``.venv`` yet, so the system python runs it.

    python -m src.launching.repoint_cards -MigrateFrom <old folder> [-WhatIf]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parents[2]
REGISTRY = Path("config") / "apps.json"
SIBLING_SUFFIXES = ("-new", "-e2e")


@dataclass(frozen=True)
class Outcome:
    """One suffixed-folder card: ``repointed``, ``would repoint`` or ``left``."""

    state: str
    name: str
    old: str
    detail: str


def _where(path: object) -> str:
    """The file or folder a path reaches, so a folder link and its target compare equal."""
    return os.path.normcase(os.path.realpath(str(path)))


def canonical_target(bat_path: str, app_dir: Path) -> Optional[Path]:
    """The same file under ``app_dir`` when ``bat_path`` lies in a suffixed sibling of it, else None.

    The card's own spelling of the parent folder is kept (a junctioned drive stays as the owner
    wrote it), so a later Apps scan sees the same string and adds no second card.
    """
    names = {(app_dir.name + suffix).lower() for suffix in SIBLING_SUFFIXES}
    path = Path(os.path.normpath(bat_path))
    for folder in path.parents:
        if folder.name.lower() in names and _where(folder.parent) == _where(app_dir.parent):
            return folder.with_name(app_dir.name) / path.relative_to(folder)
    return None


def repoint(registry: Path, app_dir: Path, *, what_if: bool = False) -> List[Outcome]:
    """Repoint the suffixed-folder cards in ``registry``; raises ``OSError`` / ``ValueError``.

    The document is edited as plain JSON, never through the app's row model, so a row or field
    this version does not know survives the write.
    """
    data = json.loads(registry.read_text(encoding="utf-8"))
    rows = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("no apps list")
    cards = [row for row in rows if isinstance(row, dict) and isinstance(row.get("bat_path"), str)]
    outcomes: List[Outcome] = []
    for card in cards:
        old = card["bat_path"]
        target = canonical_target(old, app_dir) if old else None
        if target is None:
            continue
        name = str(card.get("name") or card.get("id") or "unnamed card")
        if not target.is_file():
            outcomes.append(Outcome("left", name, old, f"{target} does not exist"))
        elif any(_where(other["bat_path"]) == _where(target) for other in cards if other is not card):
            outcomes.append(Outcome("left", name, old, f"another card already starts {target}"))
        else:
            card["bat_path"] = str(target)
            outcomes.append(Outcome("would repoint" if what_if else "repointed", name, old, str(target)))
    if not what_if and any(o.state == "repointed" for o in outcomes):
        atomic_write_json(registry, data)
    return outcomes


def main(argv: Optional[List[str]] = None, *, app_dir: Path = APP_DIR) -> int:
    """Takes ``setup.bat``'s own arguments; 1 when the cards could not be checked or written."""
    parser = argparse.ArgumentParser(prog="python -m src.launching.repoint_cards", add_help=False,
                                     allow_abbrev=False)
    parser.add_argument("-MigrateFrom", "--migrate-from", dest="old")
    parser.add_argument("-WhatIf", "--what-if", dest="what_if", action="store_true")
    args, _ = parser.parse_known_args(argv)
    registry = app_dir / REGISTRY
    if args.what_if and not registry.is_file() and args.old:
        registry = Path(args.old) / REGISTRY  # nothing is copied yet: this is what would be
    if not registry.is_file():
        logger.info("app cards: none to repoint (no %s)", REGISTRY.as_posix())
        return 0
    try:
        outcomes = repoint(registry, app_dir, what_if=args.what_if)
    except (OSError, ValueError) as exc:
        logger.error("❌ app cards: not checked, %s could not be read or written (%s)",
                     REGISTRY.as_posix(), type(exc).__name__)
        return 1
    for outcome in outcomes:
        if outcome.state == "left":
            logger.warning("⚠️ app card %r: left at %s: %s", outcome.name, outcome.old, outcome.detail)
        else:
            logger.info("app card %r: %s: %s -> %s", outcome.name, outcome.state, outcome.old, outcome.detail)
    if not outcomes:
        logger.info("app cards: none points at a %s copy of this folder", " or ".join(SIBLING_SUFFIXES))
    return 0


if __name__ == "__main__":
    # stderr: `setup.bat -MigrateFrom -Json` prints one JSON document on stdout.
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    sys.exit(main())
