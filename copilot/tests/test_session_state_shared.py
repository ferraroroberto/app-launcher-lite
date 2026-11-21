"""session_state.py --lite-sessions-only: on a shared machine, only lite's own sessions are recorded.

Run:  python -m unittest discover -s tests -v

Drives the real hook entry point with a loopback stub of app-launcher-lite's
session-host list (`FLEET_LITE_SESSION_HOST`), so membership decides the write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SharedModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name)
        body = json.dumps({"sessions": [{"session_id": "lite-1"}]}).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/sessions"

    def run_hook(self, sid: str, *flags: str, url: str = "") -> dict:
        env = {**os.environ, "COPILOT_HOOKS_STATE_DIR": str(self.state), "APP_LAUNCHER_SESSION_ID": sid,
               "FLEET_LITE_SESSION_HOST": url or self.url, "PYTHONUTF8": "1"}
        payload = json.dumps({"sessionId": f"cop-{sid or 'none'}", "cwd": str(self.state), "prompt": "hi"})
        proc = subprocess.run([sys.executable, str(REPO / "hooks" / "session_state.py"), "userPromptSubmitted", *flags],
                              input=payload, capture_output=True, text=True, timeout=30, env=env,
                              creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.state / "sessions-state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_a_lite_session_is_recorded(self) -> None:
        self.assertEqual(self.run_hook("lite-1", "--lite-sessions-only")["cop-lite-1"]["status"], "working")

    def test_other_sessions_write_nothing(self) -> None:
        self.assertEqual(self.run_hook("full-fleet-7", "--lite-sessions-only"), {})
        self.assertEqual(self.run_hook("", "--lite-sessions-only"), {})

    def test_an_unreachable_host_writes_nothing(self) -> None:
        self.assertEqual(self.run_hook("lite-1", "--lite-sessions-only", url="http://127.0.0.1:9/sessions"), {})

    def test_without_the_flag_every_session_is_recorded(self) -> None:
        self.assertIn("cop-full-fleet-7", self.run_hook("full-fleet-7"))


if __name__ == "__main__":
    unittest.main()
