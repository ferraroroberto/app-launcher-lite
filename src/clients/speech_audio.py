"""The audio side of speech to text: what an upload is, and the one WAV shape
every speech backend reads.

Some OpenAI-shaped speech servers decode only mono 16-bit PCM WAV at 16 kHz,
while a browser records MP4 or WebM. The page converts each take to that WAV
itself; this module is the server's safety net for an upload that still
arrives in another container, converting it with ffmpeg when one is on the
search path.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from typing import Dict, Optional

from src.env_path import effective_path
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

WAV_RATE = 16000
# ffmpeg decodes a minute of speech in well under a second; this only bounds
# a wedged process.
_FFMPEG_TIMEOUT = 30.0


class AudioDecodeError(RuntimeError):
    """ffmpeg was found but could not decode the upload."""


def describe(content: bytes) -> Dict[str, object]:
    """``{"container", "rate", "channels", "bits"}`` read from the upload's
    header; the last three only for WAV. Never reads past the header."""
    head = content[:64]
    info: Dict[str, object] = {"container": "unknown", "rate": None, "channels": None, "bits": None}
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        info["container"] = "wav"
        if head[12:16] == b"fmt " and len(head) >= 36:
            _, channels, rate, _, _, bits = struct.unpack("<HHIIHH", head[20:36])
            info.update(rate=rate, channels=channels, bits=bits)
    elif head[:4] == b"\x1aE\xdf\xa3":
        info["container"] = "webm"
    elif head[4:8] == b"ftyp":
        info["container"] = "mp4"
    elif head[:4] == b"OggS":
        info["container"] = "ogg"
    return info


def is_portable_wav(info: Dict[str, object]) -> bool:
    """True for the mono 16-bit 16 kHz WAV every backend accepts."""
    return (info["container"], info["rate"], info["channels"], info["bits"]) == ("wav", WAV_RATE, 1, 16)


def silent_wav(seconds: float = 0.5) -> bytes:
    """A short silent WAV in the portable shape, for the connection test."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(WAV_RATE)
        out.writeframes(b"\x00\x00" * int(WAV_RATE * seconds))
    return buf.getvalue()


def find_ffmpeg() -> Optional[str]:
    """The ffmpeg executable on the effective search path, or None."""
    return shutil.which("ffmpeg", path=effective_path())


def to_portable_wav(content: bytes, ffmpeg: str) -> bytes:
    """Convert any audio ffmpeg reads to the portable WAV.

    The upload goes through a temporary file, not a pipe: an MP4 whose index
    sits at the end cannot be read from a stream. Raises
    :class:`AudioDecodeError` when ffmpeg cannot read it.
    """
    fd, src = tempfile.mkstemp(prefix="lite-speech-", suffix=".audio")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", src,
             "-ac", "1", "-ar", str(WAV_RATE), "-sample_fmt", "s16", "-f", "wav", "pipe:1"],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioDecodeError(f"ffmpeg could not run ({type(exc).__name__}: {exc})") from exc
    finally:
        try:
            os.unlink(src)
        except OSError:
            logger.warning("⚠️ could not remove the temporary audio file %s", src)
    if proc.returncode != 0 or not proc.stdout:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AudioDecodeError(lines[-1] if lines else f"ffmpeg exited {proc.returncode}")
    return proc.stdout
