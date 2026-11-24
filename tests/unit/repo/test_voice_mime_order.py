"""The dictation recording contract: every take is uploaded as 16 kHz mono
16-bit WAV, and the recorder's own container is only the labelled fallback.

Some speech backends decode nothing but that WAV and answer a browser's MP4
or WebM take with an opaque HTTP 500, so a regression that uploads the
recorder's blob first breaks dictation with no visible cause.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static"


def test_takes_upload_as_wav_and_the_recorder_container_is_the_fallback() -> None:
    voice = (STATIC / "voice.js").read_text(encoding="utf-8")
    upload = re.search(r"async function uploadForm\(blob\) \{(.*?)\n\}", voice, re.S)
    assert upload, "voice.js no longer builds its upload in uploadForm"
    body = upload.group(1)
    assert body.index("takeToWav(blob") < body.index("'recording.wav'") < body.index("'recording.' + ext")
    assert "console.info" in body, "the fallback upload must say it is one"
    assert "uploadForm(blob)" in voice


def test_the_wav_is_mono_16_bit_at_16_khz() -> None:
    wav = (STATIC / "shared" / "voice-wav.js").read_text(encoding="utf-8")
    assert "const WAV_RATE = 16000;" in wav
    assert "new Offline(1, frames, WAV_RATE)" in wav
    assert "view.setUint16(22, 1, true); // mono" in wav
    assert "view.setUint16(34, 16, true); // bits per sample" in wav


def test_mp4_recorder_types_precede_webm_for_the_fallback() -> None:
    source = (STATIC / "voice.js").read_text(encoding="utf-8")
    ladder = re.search(r"candidates = \[(.*?)\];", source, re.S)
    assert ladder, "pickAudioMime no longer declares a candidates list"
    types = re.findall(r"'(audio/[^']+)'", ladder.group(1))
    first_webm = next((i for i, t in enumerate(types) if "webm" in t), len(types))
    assert first_webm > 0 and all("mp4" in t for t in types[:first_webm])
