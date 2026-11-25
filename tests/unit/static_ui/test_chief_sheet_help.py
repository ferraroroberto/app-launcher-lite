"""Every control in Settings > Chief carries a plain one-line explanation: its
``aria-describedby`` names a non-empty ``field-help`` line in the same sheet."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List

_SHEETS = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static" / "views" / "settings-sheets.html"


class _ChiefSheet(HTMLParser):
    """Collects the controls and help lines inside ``<dialog id="chiefSheet">``."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.controls: List[Dict[str, str]] = []
        self.help: Dict[str, str] = {}
        self._help_id = ""

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "dialog" and a.get("id") == "chiefSheet":
            self.depth = 1
            return
        if not self.depth:
            return
        if tag == "dialog":
            self.depth += 1
        is_switch = tag == "button" and a.get("role") == "switch"
        is_field = tag in ("input", "select")
        if is_switch or is_field or "model-combo-trigger" in a.get("class", ""):
            self.controls.append(a)
        if tag == "p" and "field-help" in a.get("class", "").split():
            self._help_id = a.get("id", "")
            self.help.setdefault(self._help_id, "")

    def handle_endtag(self, tag):
        if self.depth and tag == "dialog":
            self.depth -= 1
        if tag == "p":
            self._help_id = ""

    def handle_data(self, data):
        if self.depth and self._help_id:
            self.help[self._help_id] += data


def test_every_chief_control_is_explained():
    sheet = _ChiefSheet()
    sheet.feed(_SHEETS.read_text(encoding="utf-8"))
    assert sheet.controls, "the Chief sheet was not found"
    unexplained = []
    for control in sheet.controls:
        ids = control.get("aria-describedby", "").split()
        if not any(sheet.help.get(i, "").strip() for i in ids):
            unexplained.append(control.get("id") or control.get("aria-label"))
    assert unexplained == []
    ids = {c.get("id") for c in sheet.controls}
    assert {"chiefFallbackMinutes", "chiefReringSeconds", "chiefUnreadAlertMinutes",
            "chiefReofferMinutes", "chiefHoldMinutes"} <= ids
