"""Regression: ``save_webapp_config`` persists *every* ``WebappConfig`` field.

Issue #441 shipped a knob that reached the dataclass and the loader but not
the hand-written ``payload`` dict in ``save_webapp_config`` — so a Save was
in-memory only and silently reverted on restart. Issue #16 removed that dict
in favour of ``dataclasses.asdict(cfg)``, which is exhaustive by
construction; this test is what keeps it that way, failing loudly if anyone
reintroduces a hand-maintained key list (or adds a field JSON can't encode).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from src.webapp_config import WebappConfig, save_webapp_config


def test_every_dataclass_field_reaches_disk(tmp_path: Path):
    target = tmp_path / "webapp_config.json"
    cfg = WebappConfig()

    save_webapp_config(cfg, target)

    on_disk = json.loads(target.read_text(encoding="utf-8"))
    expected = {f.name for f in dataclasses.fields(WebappConfig)}
    assert set(on_disk) == expected
    assert on_disk == dataclasses.asdict(cfg)
