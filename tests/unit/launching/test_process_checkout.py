"""Two checkouts on one port: only a process running from this checkout counts as ours.

A second clone at the same commit answers ``/healthz`` and ``/api/version`` exactly like this one,
so the restart check and the webapp adoption both have to ask which tree the answering process
runs from. The servers here are real processes started with another checkout as their working
directory, which is how the tray starts a webapp.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from app.webapp.manager import PROJECT_ROOT, WebappManager, WebappManagerConfig
from src.launching import process_checkout, restart_check
from src.subprocess_flags import NO_WINDOW

HEAD = "86224f1b"

_SERVER = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"git_sha": sys.argv[2], "pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

srv = HTTPServer(("127.0.0.1", int(sys.argv[1])), H)
print(os.getpid(), flush=True)
srv.serve_forever()
"""


@pytest.fixture
def serve(tmp_path: Path) -> Iterator:
    """``serve(cwd)`` starts a webapp stand-in in ``cwd``; returns ``(port, pid)``."""
    script = tmp_path / "fake_webapp.py"
    script.write_text(_SERVER, encoding="utf-8")
    procs: List["subprocess.Popen[str]"] = []

    def _start(cwd: Path) -> Tuple[int, int]:
        cwd.mkdir(parents=True, exist_ok=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, str(script), str(port), HEAD],
            cwd=str(cwd), stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
        )
        procs.append(proc)
        assert proc.stdout is not None
        return port, int(proc.stdout.readline())

    yield _start
    for proc in procs:
        proc.kill()
        proc.wait(timeout=10)


class TestPortOwner:
    def test_names_the_other_checkout_and_recognises_this_one(self, tmp_path, serve) -> None:
        mine, other = tmp_path / "mine", tmp_path / "other"
        mine.mkdir()
        their_port, their_pid = serve(other)
        our_port, _ = serve(mine)

        theirs = process_checkout.port_owner(their_port, mine)
        assert theirs.state == process_checkout.FOREIGN
        assert their_pid in theirs.pids and process_checkout.same_checkout(theirs.checkout, other)
        assert process_checkout.port_owner(our_port, mine).state == process_checkout.OURS

    def test_an_unreadable_owner_is_unknown_never_ours(self, tmp_path) -> None:
        owner = process_checkout.port_owner(
            1, tmp_path, pids_of=lambda _p: [4242], checkout_of=lambda _pid: None
        )
        assert owner.state == process_checkout.UNKNOWN
        assert process_checkout.port_owner(1, tmp_path, pids_of=lambda _p: None).state == process_checkout.UNKNOWN


class TestRestartCheck:
    def test_a_new_pid_at_head_from_another_checkout_fails(self, tmp_path, serve) -> None:
        mine, other = tmp_path / "mine", tmp_path / "other"
        mine.mkdir()
        port, pid = serve(other)
        ok, reason = restart_check.decide({"pid": None}, restart_check.fetch_version(port), HEAD, root=mine)
        assert not ok
        assert f"pid {pid} runs from another checkout" in reason and str(other) in reason

    def test_the_same_answer_from_this_checkout_passes(self, tmp_path, serve) -> None:
        mine = tmp_path / "mine"
        port, pid = serve(mine)
        ok, reason = restart_check.decide({"pid": None}, restart_check.fetch_version(port), HEAD, root=mine)
        assert ok, reason
        assert f"pid {pid}" in reason and str(mine.resolve()) in reason


class TestAdoption:
    def _manager(self, port: int) -> WebappManager:
        return WebappManager(WebappManagerConfig(host="127.0.0.1", port=port, startup_timeout_seconds=2.0))

    def test_another_checkouts_webapp_is_refused_not_adopted(self, tmp_path, serve, monkeypatch) -> None:
        import app.webapp.manager as manager_mod

        port, pid = serve(tmp_path / "other")
        spawned = []
        monkeypatch.setattr(manager_mod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
        with pytest.raises(RuntimeError) as exc:
            self._manager(port).start(wait=False)
        message = str(exc.value)
        assert f"pid {pid} from another checkout" in message and "not adopted" in message
        assert "--replace-other-checkout" in message
        assert not spawned

    def test_this_checkouts_webapp_is_adopted(self, serve) -> None:
        port, _pid = serve(PROJECT_ROOT)
        status = self._manager(port).start(wait=False)
        assert status.running is True
