"""app-launcher-lite on non-default ports: every caller reaches Lite's own ports (stdlib unittest).

Run:  python -m unittest discover -s tests -v

Lite's `config/config.json` is a temp file naming a loopback stub's port, handed to
each caller through `FLEET_LITE_CONFIG`. A caller that still used 8465/8466 would
miss the stub, so each test asserts the stub saw the request. Nothing here reads
the real Lite config or contacts a real launcher.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))
sys.path.insert(0, str(REPO / "tests"))

import lite_ports  # noqa: E402
from test_chief_lanes import NO_WINDOW, StubLauncher, _card  # noqa: E402

LIB = REPO / "skills" / "_lib"


def _port_of(stub: StubLauncher) -> int:
    return stub.server.server_address[1]


class PortsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.config = self.tmp / "config.json"

    def write_config(self, webapp: object) -> None:
        self.config.write_text(json.dumps({"webapp": webapp}), encoding="utf-8")

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "FLEET_LITE_SESSION_HOST"}
        env.update({"FLEET_LITE_CONFIG": str(self.config), "COPILOT_HOOKS_STATE_DIR": str(self.state),
                    "APP_LAUNCHER_SESSION_ID": "chief-sid", "PYTHONUTF8": "1"})
        env.update(extra)
        return env

    def run_py(self, *args: str, stdin: str = "", **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], input=stdin, capture_output=True, text=True, timeout=60,
                              env=self.env(**extra), creationflags=NO_WINDOW, cwd=self.tmp)


class ResolverTests(PortsCase):
    def test_the_configured_ports_are_read(self) -> None:
        self.write_config({"port": 9101, "session_host_port": 9102})
        self.assertEqual(lite_ports.webapp_url(self.config), "http://127.0.0.1:9101")
        self.assertEqual(lite_ports.session_host_url(self.config), "http://127.0.0.1:9102/sessions")

    def test_no_override_keeps_the_default_ports(self) -> None:
        for webapp in ({}, {"enabled": True}, None, [], "x"):
            self.write_config(webapp)
            self.assertEqual(lite_ports.webapp_url(self.config), "http://127.0.0.1:8465")
            self.assertEqual(lite_ports.session_host_url(self.config), "http://127.0.0.1:8466/sessions")
        missing = self.tmp / "absent.json"
        self.assertEqual(lite_ports.webapp_port(missing), 8465)
        self.assertEqual(lite_ports.session_host_port(missing), 8466)

    def test_an_unusable_value_or_file_falls_back_per_port(self) -> None:
        self.write_config({"port": 70000, "session_host_port": "9102"})
        with self.assertLogs(lite_ports.logger, "WARNING"):
            self.assertEqual(lite_ports.webapp_port(self.config), 8465)
        self.assertEqual(lite_ports.session_host_port(self.config), 9102)
        self.config.write_text("{not json", encoding="utf-8")
        with self.assertLogs(lite_ports.logger, "WARNING"):
            self.assertEqual(lite_ports.webapp_port(self.config), 8465)

    def test_the_env_override_names_the_file_and_the_own_config_is_the_default(self) -> None:
        self.write_config({"port": 9101})
        with mock.patch.dict(os.environ, {"FLEET_LITE_CONFIG": str(self.config)}):
            self.assertEqual(lite_ports.config_path(), self.config)
            self.assertEqual(lite_ports.webapp_port(), 9101)
        with mock.patch.dict(os.environ, {"FLEET_LITE_CONFIG": ""}):
            self.assertEqual(lite_ports.config_path(), REPO.parent / "config" / "config.json")

    def test_the_installer_probe_reads_the_same_ports(self) -> None:
        self.write_config({"port": 9101, "session_host_port": 9102})
        for arg, expected in (("session-host-url", "http://127.0.0.1:9102/sessions"), ("webapp-port", "9101")):
            proc = self.run_py(str(LIB / "lite_ports.py"), arg)
            self.assertEqual((proc.returncode, proc.stdout.strip()), (0, expected), proc.stderr)
        self.assertEqual(self.run_py(str(LIB / "lite_ports.py"), "nope").returncode, 2)


class HookTests(PortsCase):
    def test_the_hook_asks_the_configured_session_host(self) -> None:
        stub = StubLauncher()
        self.addCleanup(stub.close)
        stub.routes["/sessions"] = (200, {"sessions": [{"session_id": "lite-1"}]})
        self.write_config({"port": 1, "session_host_port": _port_of(stub)})
        hook = str(REPO / "hooks" / "session_state.py")
        payload = json.dumps({"sessionId": "cop-1", "cwd": str(self.tmp), "prompt": "hi"})
        for sid, recorded in (("lite-1", True), ("other", False)):
            (self.state / "sessions-state.json").unlink(missing_ok=True)
            proc = self.run_py(hook, "userPromptSubmitted", "--lite-sessions-only", stdin=payload,
                               APP_LAUNCHER_SESSION_ID=sid)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((self.state / "sessions-state.json").exists(), recorded, sid)
        self.assertEqual(set(stub.paths), {"/sessions"})


class ChiefTests(PortsCase):
    """The chief's calls, with no `--base-url`, reach the stub on Lite's configured port."""

    def setUp(self) -> None:
        super().setUp()
        self.stub = StubLauncher()
        self.addCleanup(self.stub.close)
        self.stub.routes.update({
            "/api/board": (200, {"columns": {"claude_turn": [_card("lane-9", "busy")], "your_turn": []}}),
            "/api/board/chief/settings": (200, {"settings": {"worker_cap": 2, "worker_cap_scope": "all"}}),
            "/api/board/chief-settings": (200, {"settings": {}}),
            "/api/jobs": (200, {"jobs": []}),
            "/api/config": (200, {"chief_doorbell": True, "gitlab_group": "team-group"}),
            "/api/board/issues/start": (200, {"session": {"session_id": "lane-1"}}),
        })
        self.write_config({"port": _port_of(self.stub), "session_host_port": 1})

    def ops(self, *argv: str) -> subprocess.CompletedProcess:
        return self.run_py(str(LIB / "chief_ops.py"), *argv)

    def test_board_sessions_inbox_and_the_doorbell_reach_lite(self) -> None:
        board = self.ops("board")
        self.assertEqual(board.returncode, 0, board.stdout + board.stderr)
        sessions = self.ops("sessions")
        self.assertEqual(sessions.returncode, 0, sessions.stdout + sessions.stderr)
        self.assertIn("busy", sessions.stdout)
        inbox = self.ops("inbox")
        self.assertEqual(inbox.returncode, 0, inbox.stdout + inbox.stderr)
        wake = self.ops("wake")
        self.assertEqual(wake.returncode, 0, wake.stdout + wake.stderr)
        self.assertIn("WAKE=doorbell doorbell=on", wake.stdout)
        self.assertTrue({"/api/board", "/api/config"} <= set(self.stub.paths), self.stub.paths)

    def test_dispatch_posts_to_lite(self) -> None:
        driver = ("import sys; import chief_ops; from chief import launcher; "
                  "launcher.fetch_issue_state = lambda *a, **k: {'repo': 'demo', 'number': 7, 'labels': []}; "
                  "sys.exit(chief_ops.main(['dispatch', 'demo', '7']))")
        proc = self.run_py("-c", driver, PYTHONPATH=str(LIB))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPATCHED session=lane-1", proc.stdout)
        self.assertEqual([p for p in self.stub.paths if p.endswith("/start")], ["/api/board/issues/start"])

    def test_the_gitlab_namespace_comes_from_lite(self) -> None:
        driver = "import gitlab_namespace as g; print(g.DEFAULT_BASE_URL, g.from_config())"
        proc = self.run_py("-c", driver, PYTHONPATH=str(LIB))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"http://127.0.0.1:{_port_of(self.stub)} ('team-group', '')", proc.stdout)


if __name__ == "__main__":
    unittest.main()
