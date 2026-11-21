"""Tests for the lite chief mailbox: hooks/chief_inbox.py and chief_ops inbox/ack.

Run:  python -m unittest discover -s tests -v

Hermetic: every case points COPILOT_HOOKS_STATE_DIR at a temp folder. The
contract is fleet-config's docs/chief-mailbox.md v2; the ported reader
(skills/_lib/chief/mailbox.py) claiming what the lite writer published is the
cross-check.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "skills" / "_lib"))

import chief_inbox  # noqa: E402
import chief_managed  # noqa: E402
import chief_ops  # noqa: E402


def _ops(*argv: str) -> tuple:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = chief_ops.main(list(argv))
    return code, out.getvalue()


class MailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name)
        self.inbox = self.state / "chief-inbox"
        self.transcript = self.state / "events.jsonl"
        self.transcript.write_text(
            json.dumps({"type": "assistant.message", "data": {"content": "first"}}) + "\n"
            + json.dumps({"type": "assistant.message", "data": {"content": "Gate PASS, PR open."}}) + "\n"
            + json.dumps({"type": "assistant.turn_end", "data": {}}) + "\n",
            encoding="utf-8")
        env = {"COPILOT_HOOKS_STATE_DIR": str(self.state), "APP_LAUNCHER_SESSION_ID": "sid-1",
               "APP_LAUNCHER_AGENT": "copilot"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # `inbox` adds the chief's compact-due line from a launcher read; keep it off the network.
        due = mock.patch("chief.lite_compact.chief_compact_status", return_value=(None, "COMPACT_OFF"))
        due.start()
        self.addCleanup(due.stop)

    def _events(self) -> list:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.inbox.glob("*.json"))]

    def test_unmanaged_and_non_launcher_sessions_write_nothing(self) -> None:
        self.assertIsNone(chief_inbox.record({}, "agentStop"))  # no registry at all
        chief_managed.mark("other-sid", "fleet-config-lite", 1)
        self.assertIsNone(chief_inbox.record({}, "agentStop"))
        with mock.patch.dict(os.environ, {"APP_LAUNCHER_SESSION_ID": ""}):
            self.assertIsNone(chief_inbox.record({}, "agentStop"))
        self.assertFalse(self.inbox.exists())

    def test_undetermined_registry_writes_nothing(self) -> None:
        (self.state / "chief-managed.json").write_text("{not json", encoding="utf-8")
        row, reason = chief_inbox.managed_row("sid-1")
        self.assertEqual((row, reason), (None, chief_inbox.UNDETERMINED))
        self.assertIsNone(chief_inbox.record({}, "agentStop"))

    def test_turn_end_event_matches_the_v2_schema(self) -> None:
        chief_managed.mark("sid-1", "fleet-config-lite", 102, dispatch_id="d" * 32)
        path = chief_inbox.record({"transcript_path": str(self.transcript)}, "agentStop")
        event = json.loads(path.read_text(encoding="utf-8"))
        self.assertRegex(path.name, r"^\d+-[0-9a-f]{32}\.json$")
        self.assertEqual(path.name.split("-", 1)[1], event["id"] + ".json")
        self.assertEqual({k: event[k] for k in ("v", "kind", "event", "launcher_sid", "repo", "number",
                                                 "agent", "dispatch_id", "excerpt", "excerpt_source")},
                         {"v": 2, "kind": "turn-end", "event": "Stop", "launcher_sid": "sid-1",
                          "repo": "fleet-config-lite", "number": 102, "agent": "copilot",
                          "dispatch_id": "d" * 32, "excerpt": "Gate PASS, PR open.",
                          "excerpt_source": "transcript"})
        self.assertNotIn("excerpt_unavailable", event)
        self.assertRegex(event["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_second_turn_end_is_debounced_but_session_end_is_not(self) -> None:
        chief_managed.mark("sid-1", "fleet-config-lite", 102)
        self.assertIsNotNone(chief_inbox.record({}, "agentStop"))
        self.assertIsNone(chief_inbox.record({}, "agentStop"))
        end = json.loads(chief_inbox.record({}, "sessionEnd").read_text(encoding="utf-8"))
        self.assertEqual((end["kind"], end["event"], end["excerpt"]), ("session-end", "SessionEnd", None))
        self.assertIn("excerpt_unavailable", end)
        first = self._events()[0]
        self.assertEqual(first["excerpt_unavailable"], "no transcriptPath in the payload")

    def test_inbox_claims_and_ack_retires(self) -> None:
        chief_managed.mark("sid-1", "fleet-config-lite", 102)
        chief_inbox.record({"transcript_path": str(self.transcript)}, "agentStop")
        chief_inbox.record({}, "sessionEnd")
        self.assertEqual(_ops("inbox", "--count"), (0, "PENDING=2\n"))
        code, out = _ops("inbox")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("INBOX=events n=2"))
        self.assertIn("Gate PASS, PR open.", out)
        ids = [e["id"] for e in self._claimed()]
        self.assertEqual(_ops("ack", *ids), (0, "ACKED=2\n"))
        self.assertEqual(_ops("inbox")[1].strip(), "INBOX=empty")
        self.assertEqual(_ops("ack", "nope"), (1, "ACKED=0\nACK_MISSING=nope\n"))

    def _claimed(self) -> list:
        return [json.loads(p.read_text(encoding="utf-8")) for p in (self.inbox / ".claimed").glob("*.json")]

    def test_session_state_hook_publishes_through_the_real_entry_point(self) -> None:
        chief_managed.mark("sid-1", "fleet-config-lite", 102)
        payload = json.dumps({"sessionId": "c1", "cwd": str(self.state),
                              "transcriptPath": str(self.transcript), "stopReason": "end_turn"})
        proc = subprocess.run([sys.executable, str(REPO / "hooks" / "session_state.py"), "agentStop"],
                              input=payload, capture_output=True, text=True, timeout=30,
                              env={**os.environ, "PYTHONUTF8": "1"},
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 0)
        events = self._events()
        self.assertEqual([(e["kind"], e["excerpt"]) for e in events], [("turn-end", "Gate PASS, PR open.")])
        rows = json.loads((self.state / "sessions-state.json").read_text(encoding="utf-8"))
        self.assertEqual(rows["c1"]["status"], "needs-you")


if __name__ == "__main__":
    unittest.main()
