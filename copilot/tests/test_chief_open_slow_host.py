"""`open` and `sessions` against a launcher slower than the chief helper's budget.

Every slow path is an injected delay on the loopback stub with the client budgets patched
down, so each case runs in about a second and never needs a real session-host.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from test_chief_lanes import StubbedLauncherCase, _card, _ops

from chief import lite_lanes, lite_open

SLOW = 0.6  # the injected delay, longer than every patched budget below
BUDGET = 0.2


class FakeLaunches:
    """The launcher's request-id contract for one project, with a slowed spawn: a new id
    spawns once (after `delay`), a repeat answers from its record, never spawning again."""

    def __init__(self, project: str, sid: str, delay: float = 0.0) -> None:
        self.project, self.sid, self.delay = project, sid, delay
        self.records: dict = {}
        self.spawns = 0
        self._lock = threading.Lock()

    def launch(self, _method: str, body: dict) -> tuple:
        rid = body["request_id"]
        with self._lock:
            record = self.records.get(rid)
            if record is None:
                self.records[rid] = {"state": "pending"}
        if record is not None:
            if record["state"] == "pending":
                return 202, {"request_id": rid, "state": "pending"}
            return 200, {"session": {"session_id": self.sid}, "request_id": rid, "resumed": True}
        time.sleep(self.delay)
        with self._lock:
            self.spawns += 1
            self.records[rid] = {"state": "done", "session": {"session_id": self.sid}}
        return 200, {"session": {"session_id": self.sid}, "request_id": rid, "resumed": False}

    def install(self, routes: dict, request_ids: list) -> None:
        routes[f"/api/apps/{self.project}/launch"] = self.launch
        for rid in request_ids:
            routes[f"/api/apps/{self.project}/launch/{rid}"] = (
                lambda _m, _b, rid=rid: (200, {"request_id": rid, **self.records[rid]})
                if rid in self.records else (404, {"detail": "unknown launch request"}))


class SlowHostCase(StubbedLauncherCase):
    def setUp(self) -> None:
        super().setUp()
        self.stub.routes.update({
            "/api/apps": (200, {"apps": [{"id": p, "name": p, "kind": "claude-code"} for p in ("demo", "other")]}),
            "/api/board": (200, {"columns": {"claude_turn": [], "your_turn": []}}),
            "/api/board/chief/settings": (200, {"settings": {"worker_cap": 4, "worker_cap_scope": "all"}}),
        })
        self.message = self.state / "message.txt"
        self.message.write_text("Say hello to the team.", encoding="utf-8")
        for name, value in (("LAUNCH_TIMEOUT", BUDGET), ("INPUT_TIMEOUT", BUDGET),
                            ("LAUNCH_POLL_WINDOW", 2.0), ("LAUNCH_POLL_INTERVAL", 0.05)):
            patcher = mock.patch.object(lite_open, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _project(self, project: str, sid: str, delay: float = 0.0, rids: tuple = ("rid-1",)) -> FakeLaunches:
        fake = FakeLaunches(project, sid, delay)
        fake.install(self.stub.routes, list(rids))
        self.stub.routes[f"/api/claude-code/sessions/{sid}/input"] = (200, {"delivered": True, "reason": "unverified"})
        return fake

    def _transcript(self, sid: str, *texts: str) -> None:
        self.stub.routes[f"/api/claude-code/sessions/{sid}/transcript"] = (
            200, {"available": True, "entries": [{"kind": "user", "text": t} for t in texts]})

    def _open(self, project: str, *extra: str) -> tuple:
        return _ops("open", project, "--message-file", str(self.message), "--settle", "0",
                    "--timeout", "0.3", "--poll-interval", "0.05", "--base-url", self.stub.url, *extra)

    def _inputs(self, sid: str) -> int:
        return sum(1 for path, _ in self.stub.posts if path == f"/api/claude-code/sessions/{sid}/input")

    @staticmethod
    def _facts(out: str) -> dict:
        names = ("LAUNCHED", "PROMPT_DELIVERED", "EXCHANGE_CONFIRMED")
        return {line.split("=")[0]: line.split("=")[1].split()[0]
                for line in out.splitlines() if line.split("=")[0] in names}


class OpenAgainstASlowHostTests(SlowHostCase):
    def test_a_send_slower_than_the_budget_is_confirmed_by_the_transcript(self) -> None:
        self._project("demo", "s-1")
        self.stub.delays["/api/claude-code/sessions/s-1/input"] = SLOW
        self._transcript("s-1", "Say hello to the team.")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._facts(out), {"LAUNCHED": "yes", "PROMPT_DELIVERED": "yes", "EXCHANGE_CONFIRMED": "yes"})
        self.assertIn("PROMPT_DELIVERED=yes reason=confirmed by the transcript", out)
        self.assertEqual(self._inputs("s-1"), 1, "an unanswered send is never resent")

    def test_an_unanswered_send_with_no_turn_is_unknown_not_success_or_absence(self) -> None:
        self._project("demo", "s-1")
        self.stub.delays["/api/claude-code/sessions/s-1/input"] = SLOW
        self._transcript("s-1")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 1, out)
        self.assertEqual(self._facts(out), {"LAUNCHED": "yes", "PROMPT_DELIVERED": "unknown", "EXCHANGE_CONFIRMED": "no"})
        self.assertNotIn("OPENED", out)
        self.assertEqual(self._inputs("s-1"), 1)

    def test_an_unreadable_transcript_leaves_both_later_facts_unknown(self) -> None:
        self._project("demo", "s-1")
        self.stub.delays["/api/claude-code/sessions/s-1/input"] = SLOW
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 1, out)
        self.assertEqual(self._facts(out), {"LAUNCHED": "yes", "PROMPT_DELIVERED": "unknown", "EXCHANGE_CONFIRMED": "unknown"})

    def test_a_launch_slower_than_the_budget_is_polled_by_its_request_id(self) -> None:
        fake = self._project("demo", "s-1", delay=SLOW)
        self._transcript("s-1", "Say hello to the team.")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._facts(out)["LAUNCHED"], "yes")
        self.assertEqual(fake.spawns, 1)
        launches = [p for p, _ in self.stub.posts if p == "/api/apps/demo/launch"]
        self.assertEqual(len(launches), 1, "a timed-out launch is polled, never posted again")

    def test_a_connection_lost_after_the_launch_went_out_is_polled_not_a_crash(self) -> None:
        fake = self._project("demo", "s-1")
        self._transcript("s-1", "Say hello to the team.")

        def spawn_then_drop(method: str, body: dict) -> tuple:
            fake.launch(method, body)
            raise ConnectionAbortedError("the launcher dropped the connection after spawning")

        self.stub.routes["/api/apps/demo/launch"] = spawn_then_drop
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._facts(out)["LAUNCHED"], "yes")
        self.assertEqual(fake.spawns, 1)

    def test_a_launch_never_confirmed_reports_every_fact_unknown(self) -> None:
        self._project("demo", "s-1", delay=SLOW, rids=())
        with mock.patch.object(lite_open, "LAUNCH_POLL_WINDOW", 0.1):
            code, out = self._open("demo")
        self.assertEqual(code, 1, out)
        self.assertEqual(self._facts(out), {"LAUNCHED": "unknown", "PROMPT_DELIVERED": "unknown",
                                            "EXCHANGE_CONFIRMED": "unknown"})
        self.assertIn("OPEN=unconfirmed reason=timeout", out)
        self.assertIn("--request-id", out)


class AcknowledgedInputTests(SlowHostCase):
    """`open` against both host shapes: one whose input call answers with an input id and
    write time, and an older one that sends neither."""

    WRITTEN_AT = 1_700_000_000.0

    def _ack_host(self, sid: str) -> None:
        self.stub.routes[f"/api/claude-code/sessions/{sid}/input"] = (
            200, {"delivered": True, "reason": "unverified", "input_id": "abcdef0123456789",
                  "written_at": self.WRITTEN_AT})

    def _exchange(self, sid: str, at: float | None) -> None:
        latest = None if at is None else {"timestamp": "iso", "at": at}
        self.stub.routes[f"/api/board/sessions/{sid}/exchange"] = (
            200, {"available": False, "reason": "no_exchange", "latest_user": latest})

    def test_a_turn_after_the_acknowledged_write_confirms_without_the_text(self) -> None:
        self._project("demo", "s-1")
        self._ack_host("s-1")
        self._exchange("s-1", self.WRITTEN_AT + 2)
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._facts(out), {"LAUNCHED": "yes", "PROMPT_DELIVERED": "yes", "EXCHANGE_CONFIRMED": "yes"})
        self.assertIn("follows acknowledged input abcdef01", out)
        self.assertIn("input abcdef01", out.split("PROMPT_DELIVERED=")[1].splitlines()[0])

    def test_an_identical_earlier_prompt_is_not_taken_for_the_acknowledged_one(self) -> None:
        self._project("demo", "s-1")
        self._ack_host("s-1")
        self._exchange("s-1", self.WRITTEN_AT - 60)
        self._transcript("s-1", "Say hello to the team.")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 1, out)
        self.assertEqual(self._facts(out)["EXCHANGE_CONFIRMED"], "no")

    def test_an_older_host_is_still_confirmed_by_the_transcript_text(self) -> None:
        self._project("demo", "s-1")
        self._exchange("s-1", self.WRITTEN_AT + 2)
        self._transcript("s-1", "Say hello to the team.")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._facts(out)["EXCHANGE_CONFIRMED"], "yes")
        self.assertNotIn("acknowledged input", out)

    def test_an_older_host_with_no_text_match_never_reads_yes(self) -> None:
        self._project("demo", "s-1")
        self._exchange("s-1", self.WRITTEN_AT + 2)
        self._transcript("s-1")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 1, out)
        self.assertEqual(self._facts(out)["EXCHANGE_CONFIRMED"], "no")

    def test_an_exchange_without_the_turn_time_falls_back_to_the_transcript(self) -> None:
        self._project("demo", "s-1")
        self._ack_host("s-1")
        self.stub.routes["/api/board/sessions/s-1/exchange"] = (200, {"available": False, "reason": "no_exchange"})
        self._transcript("s-1", "Say hello to the team.")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._facts(out)["EXCHANGE_CONFIRMED"], "yes")


class ResumeTests(SlowHostCase):
    def test_repeating_an_open_with_the_same_request_id_never_opens_a_second_session(self) -> None:
        fake = self._project("demo", "s-1", delay=SLOW)
        with mock.patch.object(lite_open, "LAUNCH_POLL_WINDOW", 0.0):
            code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 1, out)
        self.assertEqual(self._facts(out)["LAUNCHED"], "unknown")
        time.sleep(SLOW)  # the first spawn finishes server-side after the caller gave up
        self.stub.routes["/api/board"] = (200, {"columns": {"claude_turn": [_card("s-1", "demo")]}})
        self._transcript("s-1")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(fake.spawns, 1, "the repeat answers from the first launch")
        self.assertIn("LAUNCHED session=s-1 repo=demo (resumed)", out)
        self.assertNotIn("REFUSED", out, "the busy project is the session being resumed")
        self.assertEqual(self._inputs("s-1"), 1, "the message goes out once, after the transcript read")

    def test_a_resumed_open_whose_message_already_landed_sends_nothing(self) -> None:
        fake = self._project("demo", "s-1")
        fake.records["rid-1"] = {"state": "done", "session": {"session_id": "s-1"}}
        self._transcript("s-1", "Say hello to the team.")
        code, out = self._open("demo", "--request-id", "rid-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._facts(out), {"LAUNCHED": "yes", "PROMPT_DELIVERED": "yes", "EXCHANGE_CONFIRMED": "yes"})
        self.assertEqual(self._inputs("s-1"), 0)
        self.assertEqual(fake.spawns, 0)

    def test_two_projects_opened_one_after_the_other_each_get_one_session(self) -> None:
        demo = self._project("demo", "s-demo", delay=SLOW, rids=("rid-demo",))
        other = self._project("other", "s-other", delay=SLOW, rids=("rid-other",))
        self._transcript("s-demo", "Say hello to the team.")
        self._transcript("s-other", "Say hello to the team.")
        for project in ("demo", "other"):
            code, out = self._open(project, "--request-id", f"rid-{project}")
            self.assertEqual(code, 0, out)
            self.assertEqual(self._facts(out)["PROMPT_DELIVERED"], "yes", out)
        self.assertEqual((demo.spawns, other.spawns), (1, 1))
        self.assertEqual((self._inputs("s-demo"), self._inputs("s-other")), (1, 1))


class RosterTests(SlowHostCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(lite_lanes, "ROSTER_BOARD_TIMEOUT", BUDGET)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sessions_answers_from_the_roster_while_the_board_is_stalled(self) -> None:
        self.stub.delays["/api/board"] = SLOW
        self.stub.routes["/api/claude-code/roster"] = (200, {"source": "session-host", "sessions": [
            {"session_id": "s-demo-123456", "project": "demo", "agent": "copilot", "label": "",
             "state": "working", "quiet_seconds": 0.4},
            {"session_id": "s-other-12345", "project": "other", "agent": "copilot", "label": "",
             "state": "unknown", "quiet_seconds": None},
        ]})
        code, out = _ops("sessions", "--base-url", self.stub.url)
        self.assertEqual(code, 0, out)
        self.assertIn(f"SOURCE=session-host reason=board did not answer within {BUDGET:g}s", out)
        self.assertIn("demo: working", out)
        self.assertIn("other: unknown", out)

    def test_a_stalled_board_and_a_silent_roster_stay_unconfirmed(self) -> None:
        self.stub.delays["/api/board"] = SLOW
        self.stub.routes["/api/claude-code/roster"] = (503, {"detail": "session-host roster not confirmed"})
        code, out = _ops("sessions", "--base-url", self.stub.url)
        self.assertNotEqual(code, 0)
        self.assertNotIn("no repo currently occupied", out)

    def test_a_board_that_answers_is_used_unchanged(self) -> None:
        self.stub.routes["/api/board"] = (200, {"columns": {"claude_turn": [_card("s-1", "demo")]}})
        code, out = _ops("sessions", "--base-url", self.stub.url)
        self.assertEqual(code, 0, out)
        self.assertNotIn("SOURCE=", out)
        self.assertNotIn("/api/claude-code/roster", self.stub.paths)


if __name__ == "__main__":
    unittest.main()
