"""The dictation recorder prefers mp4 over webm.

The local hub's parakeet and whisper backends answer HTTP 400 to a browser's
webm/opus take but read mp4, so a regression that puts webm first breaks
dictation from Chrome and Android with no visible cause.
"""

from __future__ import annotations

import re
from pathlib import Path

VOICE_JS = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static" / "voice.js"


def test_mp4_recorder_types_precede_webm() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")
    ladder = re.search(r"candidates = \[(.*?)\];", source, re.S)
    assert ladder, "pickAudioMime no longer declares a candidates list"
    types = re.findall(r"'(audio/[^']+)'", ladder.group(1))
    assert types, "the candidates list is empty"
    first_webm = next((i for i, t in enumerate(types) if "webm" in t), len(types))
    assert all("mp4" in t for t in types[:first_webm]) and first_webm > 0
    assert any("mp4" in t for t in types)
