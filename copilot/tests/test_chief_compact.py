"""Tests for the lite chief's compaction: the Copilot context reading and the verbs.

Run:  python -m unittest discover -s tests -v

The percentage comes from a Copilot session's own `session.usage_record`, joined
through the sessions-state row the hook writes; every unreadable step must stay
`unknown`. The verbs run against the loopback launcher stub from the lane tests.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chief_ops  # noqa: E402
from chief import copilot_context, lite_compact  # noqa: E402
from test_chief_lanes import StubLauncher  # noqa: E402


def _ops(*argv: str) -> tuple:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = chief_ops.main(list(argv))
    return code, out.getvalue()


def _usage(used: object, window: object) -> str:
    return json.dumps({"type": "session.usage_record", "data": {"usage": {"inputTokens": used,
                                                                          "maxPromptTokens": window}}})


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name)
        self.events = self.state / "events.jsonl"
        patcher = mock.patch.dict(os.environ, {"COPILOT_HOOKS_STATE_DIR": str(self.state),
                                               "APP_LAUNCHER_SESSION_ID": "chief-sid"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rows(self, rows: dict) -> None:
        (self.state / "sessions-state.json").write_text(json.dumps(rows), encoding="utf-8")

    def write_events(self, *lines: str) -> None:
        self.events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def row(self, launcher_sid: str = "chief-sid", stamp: str = "2026-11-17T08:00:00Z") -> dict:
        return {"launcher_session_id": launcher_sid, "transcript_path": str(self.events), "updated_at": stamp}


class ContextTests(Fixture):
    def test_newest_usage_record_of_the_newest_row(self) -> None:
        self.write_rows({"old": {**self.row(stamp="2026-11-17T07:00:00Z"), "transcript_path": "missing.jsonl"},
                         "new": self.row()})
        self.write_events(_usage(10, 100), json.dumps({"type": "assistant.message"}), _usage(136000, 272000))
        info = copilot_context.context_info("chief-sid")
        self.assertEqual((info["available"], info["percent"], info["source"]), (True, 50, "usage_record"))

    def test_falls_back_to_the_copilot_session_folder(self) -> None:
        self.write_rows({"cop-1": {**self.row(), "transcript_path": None}})
        folder = self.state / "session-state" / "cop-1"
        folder.mkdir(parents=True)
        (folder / "events.jsonl").write_text(_usage(27200, 272000) + "\n", encoding="utf-8")
        self.assertEqual(copilot_context.context_info("chief-sid", session_state=self.state / "session-state")["percent"], 10)

    def test_every_unreadable_step_is_unavailable_with_a_reason(self) -> None:
        self.assertIn("no sessions-state file", copilot_context.context_info("chief-sid")["reason"])
        self.write_rows({"x": self.row(launcher_sid="someone-else")})
        self.assertIn("no Copilot session row", copilot_context.context_info("chief-sid")["reason"])
        self.write_rows({"x": self.row()})
        self.assertIn("events.jsonl unreadable", copilot_context.context_info("chief-sid")["reason"])
        self.write_events(json.dumps({"type": "assistant.message"}))
        self.assertIn("no session.usage_record", copilot_context.context_info("chief-sid")["reason"])
        self.write_events(_usage(None, 100))
        self.assertIn("lacks inputTokens", copilot_context.context_info("chief-sid")["reason"])
        self.write_events(_usage(5, 0))
        info = copilot_context.context_info("chief-sid")
        self.assertFalse(info["available"])
        self.assertNotIn("percent", info)


class VerbTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.stub = StubLauncher()
        self.addCleanup(self.stub.close)
        chief = {"session_id": "chief-sid", "label": "chief", "alive": True, "repo": "fleet-config-lite"}
        self.stub.routes.update({
            "/api/board": (200, {"columns": {"your_turn": [chief]}}),
            "/api/board/chief-settings": (200, {"auto_compact_threshold": 40}),
            "/api/claude-code/sessions/chief-sid/input": (200, {"ok": True}),
        })
        self.write_rows({"cop-1": self.row()})

    def fresh_handover_and_plan(self) -> None:
        (self.state / "chief-handover.md").write_text("handover", encoding="utf-8")
        (self.state / "chief-plan.json").write_text("{}", encoding="utf-8")

    def test_compact_due_and_context_lines(self) -> None:
        self.write_events(_usage(150, 300))
        self.assertEqual(_ops("compact-due", "--base-url", self.stub.url),
                         (0, "COMPACT_DUE percent=50 threshold=40\n"))
        self.assertEqual(_ops("context", "--base-url", self.stub.url), (0, "CONTEXT sid=chief-si percent=50\n"))
        self.write_events(json.dumps({"type": "assistant.message"}))
        code, out = _ops("compact-due", "--base-url", self.stub.url)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("COMPACT_DUE=unknown reason=no session.usage_record"), out)

    def test_self_compact_refuses_on_unknown_and_stale_then_queues(self) -> None:
        self.write_events(json.dumps({"type": "assistant.message"}))
        code, out = _ops("self-compact", "--base-url", self.stub.url)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED=context percentage unknown", out)
        self.write_events(_usage(150, 300))
        self.assertIn("REFUSED=handover log missing", _ops("self-compact", "--base-url", self.stub.url)[1])
        self.fresh_handover_and_plan()
        code, out = _ops("self-compact", "--base-url", self.stub.url)
        self.assertEqual(code, 0, out)
        self.assertIn("COMPACT_QUEUED sid=chief-si verdict=submitted", out)
        path, body = self.stub.posts[-1]
        self.assertEqual(path, "/api/claude-code/sessions/chief-sid/input")
        self.assertTrue(body["data"].startswith("/compact Keep the in-flight lanes"))
        self.assertTrue((self.state / "chief-compact-queued").exists())

    def test_self_compact_never_targets_another_session(self) -> None:
        with mock.patch.dict(os.environ, {"APP_LAUNCHER_SESSION_ID": "lane-sid"}):
            code, out = _ops("self-compact", "--base-url", self.stub.url)
        self.assertEqual((code, self.stub.posts), (1, []))
        self.assertIn("REFUSED=the caller is not the live chief", out)

    def test_lane_compact_needs_a_fresh_handoff(self) -> None:
        with mock.patch.dict(os.environ, {"APP_LAUNCHER_SESSION_ID": "lane-sid"}):
            self.assertIn("REFUSED=handoff missing", _ops("lane-compact", "--base-url", self.stub.url)[1])
            handoff = self.state / "lane-handoffs" / "lane-sid.md"
            handoff.parent.mkdir()
            handoff.write_text("next step", encoding="utf-8")
            self.stub.routes["/api/claude-code/sessions/lane-sid/input"] = (200, {})
            code, out = _ops("lane-compact", "--base-url", self.stub.url)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.state / "lane-compact-queued" / "lane-sid").exists())
        self.assertIn("lane handoff at", self.stub.posts[-1][1]["data"])

    def test_inbox_appends_the_due_line_only_for_the_chief(self) -> None:
        with mock.patch.object(lite_compact, "chief_compact_status",
                               return_value=("chief-sid", "COMPACT_DUE percent=80 threshold=40")):
            self.assertIn("COMPACT_DUE percent=80", _ops("inbox")[1])
            with mock.patch.dict(os.environ, {"APP_LAUNCHER_SESSION_ID": "lane-sid"}):
                self.assertEqual(_ops("inbox")[1].strip(), "INBOX=empty")


if __name__ == "__main__":
    unittest.main()
