"""A fake OpenAI-shaped speech server that accepts only 16 kHz mono 16-bit WAV.

It stands in for the strict backends some machines run: ``GET /models``
answers 200, ``POST /audio/transcriptions`` transcribes a WAV of exactly that
shape and answers an opaque HTTP 500 to anything else, as those servers do.
"""

from __future__ import annotations

import json
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, List

TRANSCRIPT = "hello from a wav"


def is_strict_wav(audio: bytes) -> bool:
    """True for a RIFF/WAVE file whose fmt chunk is PCM, mono, 16 kHz, 16-bit."""
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE" or audio[12:16] != b"fmt ":
        return False
    fmt, channels, rate, _, _, bits = struct.unpack("<HHIIHH", audio[20:36])
    return (fmt, channels, rate, bits) == (1, 1, 16000, 16)


def _file_part(body: bytes, content_type: str) -> bytes:
    boundary = re.search(r"boundary=([^;]+)", content_type).group(1).strip('"').encode()
    for part in body.split(b"--" + boundary):
        head, _, rest = part.partition(b"\r\n\r\n")
        if b'name="file"' in head:
            return rest[: -2] if rest.endswith(b"\r\n") else rest
    return b""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass

    def _reply(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._reply(200, b'{"data": []}')

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        audio = _file_part(body, self.headers.get("Content-Type", ""))
        self.server.uploads.append(audio)  # type: ignore[attr-defined]
        if is_strict_wav(audio):
            self._reply(200, json.dumps({"text": TRANSCRIPT}).encode())
        else:
            self._reply(500, b"Internal Server Error", "text/plain")


def serve() -> Iterator[tuple]:
    """Yield ``(base_url, uploads)`` for a server on a free loopback port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    uploads: List[bytes] = []
    server.uploads = uploads  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", uploads
    finally:
        server.shutdown()
        server.server_close()
