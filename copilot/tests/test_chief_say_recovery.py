"""`chief_ops.py say --verify` recovers from a transient transport failure without double-sending.

The launcher is a scripted fake (`launcher._request`): each path answers from a queue, an
exception is raised, a callable runs (so a POST can land its text in the transcript and then
time out). Sleeping is patched out and recorded, so the 1 s / 2 s / 5 s backoff is asserted
rather than waited for.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Union
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))

from chief import launcher, lite_lanes  # noqa: E402

SID = "a" * 32
STEER = "Run the gate once more, then stop."
INPUT = f"/api/claude-code/sessions/{SID}/input"
TRANSCRIPT = f"/api/claude-code/sessions/{SID}/transcript"

Outcome = Union[Dict[str, Any], BaseException, Callable[[Any], Dict[str, Any]]]


def _refused() -> urllib.error.URLError:
    return urllib.error.URLError(ConnectionRefusedError(10061, "refused"))


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://127.0.0.1/x", code, "err", {}, io.BytesIO(b'{"detail": "no"}'))


def _transcript(*texts: str) -> Dict[str, Any]:
    return {"available": True, "entries": [{"kind": "user", "text": t} for t in texts]}


_ACCEPTED = {"ok": True, "delivered": True, "reason": "unverified"}


class FakeLauncher:
    """Answers each path from a queue (the last entry repeats) and records every call."""

    def __init__(self) -> None:
        self.routes: Dict[str, List[Outcome]] = {"/api/board": [{"columns": {}}]}
        self.calls: List[tuple] = []

    def __call__(self, base_url: str, path: str, method: str = "GET", body: Any = None,
                 timeout: float = 10.0) -> Dict[str, Any]:
        self.calls.append((method, path, body))
        queue = self.routes.get(path)
        if not queue:
            raise _http(404)
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(body) if callable(outcome) else outcome

    def posts(self) -> int:
        return sum(1 for method, path, _ in self.calls if method == "POST" and path.endswith("/input"))


class SayRecoveryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeLauncher()
        self.sleeps: List[float] = []
        for patcher in (
            mock.patch.object(launcher, "_request", self.fake),
            mock.patch.object(time, "sleep", self.sleeps.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.steer_file = Path(td.name) / "steer.txt"
        self.steer_file.write_text(STEER, encoding="utf-8")

    def say(self, sid: str = SID, verify: bool = True) -> tuple:
        args = SimpleNamespace(sid=sid, file=str(self.steer_file), verify=verify, timeout=0.0,
                               poll_interval=0.0, base_url="http://127.0.0.1:1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lite_lanes.cmd_say(args)
        return code, out.getvalue()

    def landed(self, then: BaseException) -> Callable[[Any], Dict[str, Any]]:
        """A POST that reaches the session (its text enters the transcript) and then fails."""
        def _land(_body: Any) -> Dict[str, Any]:
            entries = self.fake.routes[TRANSCRIPT]
            entries[:] = [_transcript(STEER)]
            raise then
        return _land


class RecoveredSendTests(SayRecoveryCase):
    def test_down_then_back_with_the_text_absent_sends_twice_and_is_delivered(self) -> None:
        self.fake.routes[INPUT] = [_refused(), _ACCEPTED]
        self.fake.routes[TRANSCRIPT] = [_refused(), _transcript(), _transcript(STEER)]
        code, out = self.say()
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("DELIVERED sid="), out)
        self.assertEqual(self.fake.posts(), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_down_then_back_with_the_text_already_there_sends_once(self) -> None:
        self.fake.routes[INPUT] = [_refused()]
        self.fake.routes[TRANSCRIPT] = [_refused(), _transcript(STEER)]
        code, out = self.say()
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("DELIVERED sid="), out)
        self.assertEqual(self.fake.posts(), 1)

    def test_an_earlier_identical_steer_is_not_taken_for_this_one(self) -> None:
        # The transcript already holds one copy before the first send: a copy still there
        # after a refused send is the old one, so the resend goes out.
        self.fake.routes[INPUT] = [_refused(), _ACCEPTED]
        self.fake.routes[TRANSCRIPT] = [_transcript(STEER), _transcript(STEER),
                                        _transcript(STEER, STEER)]
        code, out = self.say()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.posts(), 2)

    def test_a_session_host_503_is_retried(self) -> None:
        self.fake.routes[INPUT] = [_http(503), _ACCEPTED]
        self.fake.routes[TRANSCRIPT] = [_transcript(), _transcript(), _transcript(STEER)]
        code, out = self.say()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.posts(), 2)


class AmbiguousSendTests(SayRecoveryCase):
    def test_a_timeout_that_landed_is_never_sent_again(self) -> None:
        self.fake.routes[TRANSCRIPT] = [_transcript()]
        self.fake.routes[INPUT] = [self.landed(TimeoutError("timed out"))]
        code, out = self.say()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.posts(), 1)
        copies = sum(1 for e in self.fake.routes[TRANSCRIPT][0]["entries"] if e["text"] == STEER)
        self.assertEqual(copies, 1)

    def test_a_reset_that_landed_is_never_sent_again(self) -> None:
        self.fake.routes[TRANSCRIPT] = [_transcript()]
        self.fake.routes[INPUT] = [self.landed(ConnectionResetError(10054, "reset"))]
        code, _out = self.say()
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.posts(), 1)

    def test_timeouts_with_the_text_absent_stop_at_three_sends_as_pending(self) -> None:
        self.fake.routes[TRANSCRIPT] = [_transcript()]
        self.fake.routes[INPUT] = [TimeoutError("timed out")]
        code, out = self.say()
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.posts(), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0, 5.0])
        self.assertTrue(out.startswith("PENDING sid="), out)
        self.assertIn("attempts=3", out)

    def test_a_reset_with_an_unreadable_transcript_is_not_resent(self) -> None:
        # The write may have landed and nothing can say it did not: stop at one send.
        self.fake.routes[TRANSCRIPT] = [_refused()]
        self.fake.routes[INPUT] = [ConnectionResetError(10054, "reset")]
        code, out = self.say()
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.posts(), 1)
        self.assertTrue(out.startswith("UNKNOWN sid="), out)


class UnreadableTranscriptTests(SayRecoveryCase):
    def test_refused_sends_with_an_unreadable_transcript_end_unknown_after_three(self) -> None:
        self.fake.routes[TRANSCRIPT] = [_refused()]
        self.fake.routes[INPUT] = [_refused()]
        code, out = self.say()
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.posts(), 3)
        self.assertTrue(out.startswith("UNKNOWN sid="), out)
        self.assertNotIn("DELIVERED", out)
        self.assertNotIn("STRANDED", out)

    def test_a_transcript_that_reports_unavailable_is_unreadable_not_empty(self) -> None:
        self.fake.routes[TRANSCRIPT] = [{"available": False, "reason": "no_transcript", "entries": []}]
        self.fake.routes[INPUT] = [TimeoutError("timed out")]
        code, out = self.say()
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.posts(), 1)
        self.assertTrue(out.startswith("UNKNOWN sid="), out)


class AuthoritativeNegativeTests(SayRecoveryCase):
    def test_an_http_negative_is_stranded_and_never_retried(self) -> None:
        for code in (502, 409, 400, 403):
            with self.subTest(status=code):
                self.fake.calls.clear()
                self.sleeps.clear()
                self.fake.routes[TRANSCRIPT] = [_transcript()]
                self.fake.routes[INPUT] = [_http(code)]
                self.fake.routes["/api/claude-code/sessions"] = [{"sessions": []}]
                exit_code, out = self.say()
                self.assertEqual(exit_code, 1)
                self.assertEqual(self.fake.posts(), 1)
                self.assertEqual(self.sleeps, [])
                self.assertTrue(out.startswith("STRANDED sid="), out)

    def test_a_500_is_not_retried_and_stays_unknown(self) -> None:
        self.fake.routes[TRANSCRIPT] = [_transcript()]
        self.fake.routes[INPUT] = [_http(500)]
        code, out = self.say()
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.posts(), 1)
        self.assertTrue(out.startswith("UNKNOWN sid="), out)


class PlainSayTests(SayRecoveryCase):
    def test_plain_say_stays_a_single_post(self) -> None:
        self.fake.routes[INPUT] = [_refused()]
        code, out = self.say(verify=False)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("FAILED sid="), out)
        self.assertEqual(self.fake.posts(), 1)
        self.assertEqual(self.sleeps, [])


class SidResolveTests(SayRecoveryCase):
    def test_a_launcher_down_during_resolve_is_waited_out_and_the_steer_sent_once(self) -> None:
        card = {"session_id": SID, "alive": True}
        self.fake.routes["/api/board"] = [_refused(), _refused(),
                                          {"columns": {"your_turn": [card]}}]
        self.fake.routes[INPUT] = [_ACCEPTED]
        self.fake.routes[TRANSCRIPT] = [_transcript(), _transcript(STEER)]
        code, out = self.say(sid=SID[:8])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sleeps, [1.0, 2.0])
        self.assertEqual(self.fake.posts(), 1)
        posted_paths = [path for method, path, _ in self.fake.calls if method == "POST"]
        self.assertEqual(posted_paths, [INPUT])

    def test_a_board_that_never_answers_passes_the_id_through_after_three_reads(self) -> None:
        self.fake.routes["/api/board"] = [_refused()]
        sid, reason = launcher.resolve_sid_via_board("http://127.0.0.1:1", SID[:8])
        self.assertEqual((sid, reason), (SID[:8], None))
        self.assertEqual(sum(1 for _m, p, _b in self.fake.calls if p == "/api/board"), 3)

    def test_a_board_http_error_is_not_retried(self) -> None:
        self.fake.routes["/api/board"] = [_http(500)]
        launcher.resolve_sid_via_board("http://127.0.0.1:1", SID[:8])
        self.assertEqual(sum(1 for _m, p, _b in self.fake.calls if p == "/api/board"), 1)
        self.assertEqual(self.sleeps, [])


class TransientFailureTests(unittest.TestCase):
    def test_what_each_failure_proves_about_the_request(self) -> None:
        self.assertEqual(launcher.transient_failure(_refused()), launcher.NOT_SENT)
        self.assertEqual(launcher.transient_failure(_http(503)), launcher.NOT_SENT)
        self.assertEqual(launcher.transient_failure(TimeoutError()), launcher.MAYBE_SENT)
        self.assertEqual(launcher.transient_failure(ConnectionResetError()), launcher.MAYBE_SENT)
        self.assertEqual(launcher.transient_failure(urllib.error.URLError(TimeoutError())),
                         launcher.MAYBE_SENT)
        for status in (400, 403, 404, 409, 500, 502):
            self.assertIsNone(launcher.transient_failure(_http(status)), status)
        self.assertIsNone(launcher.transient_failure(ValueError("bad body")))


if __name__ == "__main__":
    unittest.main()
