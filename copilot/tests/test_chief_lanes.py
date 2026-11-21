"""Tests for the lite chief lane verbs and GitLab verify (stdlib unittest).

Run:  python -m unittest discover -s tests -v

`dispatch` runs against a loopback stub of app-launcher-lite's endpoints, so the
rails, the chief-managed mark and the unconfirmed-spawn path are exercised end
to end without spawning a real lane. glab is replaced by a fake runner.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))

import chief_ops  # noqa: E402
from chief import launcher, lite_lanes, lite_verify  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ops(*argv: str) -> tuple:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = chief_ops.main(list(argv))
    return code, out.getvalue()


def _card(sid: str, repo: str) -> dict:
    return {"session_id": sid, "repo": repo, "project": repo, "alive": True, "status": "working",
            "label": "", "cwd": f"E:/x/{repo}"}


class StubLauncher:
    """A loopback HTTP server answering the lite launcher endpoints from a dict."""

    def __init__(self) -> None:
        self.routes: dict = {}
        self.posts: list = []
        self.paths: list = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _answer(self) -> None:
                stub.paths.append(self.path)
                status, body = stub.routes.get(self.path, (404, {"detail": "no route"}))
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._answer()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                stub.posts.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                self._answer()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class StubbedLauncherCase(unittest.TestCase):
    """A scratch state dir, the chief's own session id and a loopback launcher stub per test."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name)
        env = {"COPILOT_HOOKS_STATE_DIR": str(self.state), "APP_LAUNCHER_SESSION_ID": "chief-sid"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stub = StubLauncher()
        self.addCleanup(self.stub.close)


class DispatchTests(StubbedLauncherCase):
    def setUp(self) -> None:
        super().setUp()
        self.stub.routes.update({
            "/api/board": (200, {"columns": {"claude_turn": [], "your_turn": []}}),
            "/api/board/chief/settings": (200, {"settings": {"worker_cap": 2, "worker_cap_scope": "all"}}),
            "/api/jobs": (200, {"jobs": []}),
        })
        issue = mock.patch.object(launcher, "fetch_issue_state",
                                  return_value={"repo": "demo", "number": 7, "labels": []})
        issue.start()
        self.addCleanup(issue.stop)

    def test_success_marks_the_session_chief_managed(self) -> None:
        self.stub.routes["/api/board/issues/start"] = (200, {"session": {"session_id": "lane-1"}})
        code, out = _ops("dispatch", "demo", "7", "--model", "gpt-6-luna", "--base-url", self.stub.url)
        self.assertEqual(code, 0, out)
        self.assertIn("DISPATCHED session=lane-1 repo=demo issue=7", out)
        dispatch_id = out.split("DISPATCH_ID=")[1].split()[0]
        self.assertEqual([(path, {k: v for k, v in body.items() if k != "brief"}) for path, body in self.stub.posts],
                         [("/api/board/issues/start",
                           {"repo": "demo", "number": 7, "mode": "start", "model": "gpt-6-luna"})])
        row = json.loads((self.state / "chief-managed.json").read_text(encoding="utf-8"))["lane-1"]
        self.assertEqual((row["repo"], row["number"], row["dispatch_id"]), ("demo", 7, dispatch_id))

    def test_every_dispatch_brief_opens_with_this_checkouts_skills(self) -> None:
        self.stub.routes["/api/board/issues/start"] = (200, {"session": {"session_id": "lane-1"}})
        brief = self.state / "brief.md"
        brief.write_text("Scope: only the docs.", encoding="utf-8")
        skills = (REPO / "skills").as_posix()
        for extra, mode, tail in ((), "start", ""), (("--brief-file", str(brief)), "start", "Scope: only the docs."),                 ((("--mode", "yolo", "--yolo-confirmed")), "yolo", ""):
            self.stub.posts.clear()
            code, out = _ops("dispatch", "demo", "7", "--base-url", self.stub.url, *extra)
            self.assertEqual(code, 0, out)
            sent = self.stub.posts[0][1]["brief"]
            self.assertTrue(sent.startswith("This is a fleet-config-lite lane on GitLab."), sent)
            self.assertIn(f"{skills}/issue-{mode}/SKILL.md", sent)
            self.assertIn(f"{skills}/issue-finish/SKILL.md", sent)
            self.assertTrue(sent.rstrip().endswith(tail or "denies them)."), sent)

    def test_the_preamble_does_not_make_a_brief_fleet_wide(self) -> None:
        from chief import rails
        self.assertFalse(rails.is_fleet_wide_brief(lite_lanes.lane_preamble("start")))

    def test_occupied_repo_is_refused_before_any_post(self) -> None:
        self.stub.routes["/api/board"] = (200, {"columns": {"claude_turn": [_card("other", "demo")]}})
        code, out = _ops("dispatch", "demo", "7", "--base-url", self.stub.url)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("REFUSED="), out)
        self.assertEqual(self.stub.posts, [])

    def test_needs_owner_label_is_refused(self) -> None:
        with mock.patch.object(launcher, "fetch_issue_state",
                               return_value={"repo": "demo", "number": 7, "labels": [{"name": "needs-owner"}]}):
            code, out = _ops("dispatch", "demo", "7", "--base-url", self.stub.url)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED=needs-owner", out)
        self.assertEqual(self.stub.posts, [])

    def test_a_5xx_start_is_unconfirmed_not_refused(self) -> None:
        self.stub.routes["/api/board/issues/start"] = (503, {"detail": "busy"})
        code, out = _ops("dispatch", "demo", "7", "--base-url", self.stub.url)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("DISPATCH=unconfirmed reason=http-503"), out)
        self.assertEqual(len(self.stub.posts), 1)

    def test_non_loopback_base_url_is_refused(self) -> None:
        code, _out = _ops("board", "--base-url", "http://example.com")
        self.assertEqual(code, 2)


class OpenTests(StubbedLauncherCase):
    """`open` against the loopback stub: the rails, the single POST and the delivery verdict."""

    def setUp(self) -> None:
        super().setUp()
        self.stub.routes.update({
            "/api/apps": (200, {"apps": [{"id": "demo", "name": "demo", "kind": "claude-code"},
                                         {"id": "tray", "name": "tray", "kind": "tray"}]}),
            "/api/board": (200, {"columns": {"claude_turn": [], "your_turn": []}}),
            "/api/board/chief/settings": (200, {"settings": {"worker_cap": 2, "worker_cap_scope": "all"}}),
            "/api/apps/demo/launch": (200, {"session": {"session_id": "plain-1"}}),
            "/api/claude-code/sessions/plain-1/input": (200, {"delivered": True, "reason": "unverified"}),
        })
        self.message = self.state / "message.txt"
        self.message.write_text("Say hello\nto the team.", encoding="utf-8")

    def _open(self, *extra: str) -> tuple:
        return _ops("open", "demo", "--settle", "0", "--timeout", "0", "--poll-interval", "0",
                    "--base-url", self.stub.url, *extra)

    def _user_turn(self, text: str) -> None:
        self.stub.routes["/api/claude-code/sessions/plain-1/transcript"] = (
            200, {"available": True, "entries": [{"kind": "user", "text": text}]})

    def test_without_a_message_it_opens_and_marks_nothing(self) -> None:
        code, out = self._open()
        self.assertEqual(code, 0, out)
        self.assertIn("OPENED session=plain-1 repo=demo", out)
        self.assertIn("MESSAGE=none", out)
        self.assertEqual(self.stub.posts, [("/api/apps/demo/launch", {"mode": "pty", "agent": "copilot"})])
        self.assertFalse((self.state / "chief-managed.json").exists())

    def test_message_seen_as_the_first_user_turn_is_opened_and_delivered(self) -> None:
        self._user_turn("Say hello to the team.")
        code, out = self._open("--message-file", str(self.message))
        self.assertEqual(code, 0, out)
        self.assertIn("OPENED session=plain-1 repo=demo", out)
        self.assertIn("DELIVERED sid=plain-1", out)
        self.assertEqual([path for path, _ in self.stub.posts],
                         ["/api/apps/demo/launch", "/api/claude-code/sessions/plain-1/input"])

    def test_a_message_the_launcher_accepted_but_copilot_never_got_is_stranded_not_opened(self) -> None:
        # The launcher answers `delivered: true` for text a still-drawing TUI drops; the
        # transcript staying empty is the only truth.
        self.stub.routes["/api/claude-code/sessions/plain-1/transcript"] = (
            200, {"available": False, "reason": "no_transcript", "entries": []})
        code, out = self._open("--message-file", str(self.message))
        self.assertEqual(code, 1)
        self.assertNotIn("OPENED", out)
        self.assertIn("STRANDED sid=plain-1", out)
        self.assertIn("SESSION_LIVE session=plain-1", out)
        inputs = [path for path, _ in self.stub.posts if path.endswith("/input")]
        self.assertEqual(len(inputs), 1, "the message is never resent")

    def test_an_unreadable_transcript_is_unknown_not_opened(self) -> None:
        code, out = self._open("--message-file", str(self.message))
        self.assertEqual(code, 1)
        self.assertNotIn("OPENED", out)
        self.assertIn("UNKNOWN sid=plain-1", out)

    def test_the_launcher_naming_the_input_undelivered_is_stranded(self) -> None:
        self.stub.routes["/api/claude-code/sessions/plain-1/input"] = (502, {"detail": "not echoed"})
        code, out = self._open("--message-file", str(self.message))
        self.assertEqual(code, 1)
        self.assertIn("STRANDED sid=plain-1", out)

    def test_occupied_project_is_refused_before_any_post(self) -> None:
        self.stub.routes["/api/board"] = (200, {"columns": {"claude_turn": [_card("other", "demo")]}})
        code, out = self._open()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("REFUSED=repo already has a live session"), out)
        self.assertEqual(self.stub.posts, [])

    def test_worker_cap_is_refused_before_any_post(self) -> None:
        self.stub.routes["/api/board"] = (200, {"columns": {"claude_turn": [_card("a", "x"), _card("b", "y")]}})
        code, out = self._open()
        self.assertEqual(code, 1)
        self.assertIn("REFUSED=at/over worker cap: 2/2 alive", out)
        self.assertEqual(self.stub.posts, [])

    def test_unknown_or_non_coding_project_is_refused(self) -> None:
        for repo in ("nowhere", "tray"):
            code, out = _ops("open", repo, "--base-url", self.stub.url)
            self.assertEqual(code, 1, repo)
            self.assertIn("REFUSED=no Coding project named", out)
        self.assertEqual(self.stub.posts, [])

    def test_an_empty_message_file_is_refused_before_launching(self) -> None:
        self.message.write_text("  \n", encoding="utf-8")
        code, out = self._open("--message-file", str(self.message))
        self.assertEqual(code, 1)
        self.assertIn("REFUSED=message file is empty", out)
        self.assertEqual(self.stub.posts, [])

    def test_a_launcher_refusal_is_reported_with_its_reason(self) -> None:
        self.stub.routes["/api/apps/demo/launch"] = (409, {"detail": "demo: behind origin"})
        code, out = self._open()
        self.assertEqual(code, 1)
        self.assertIn("REFUSED=the launcher refused the launch (HTTP 409)", out)

    def test_a_5xx_launch_is_unconfirmed_and_sends_no_message(self) -> None:
        self.stub.routes["/api/apps/demo/launch"] = (503, {"detail": "busy"})
        code, out = self._open("--message-file", str(self.message))
        self.assertEqual(code, 1)
        self.assertIn("OPEN=unconfirmed reason=http-503 repo=demo", out)
        self.assertEqual([path for path, _ in self.stub.posts], ["/api/apps/demo/launch"])


class FileTests(StubbedLauncherCase):
    def setUp(self) -> None:
        super().setUp()
        self.body = self.state / "body.md"
        self.body.write_text("## What\n\nSomething.\n", encoding="utf-8")

    def _file(self, *extra: str, title: str = "A title") -> tuple:
        return _ops("file", "demo", "--title", title, "--body-file", str(self.body),
                    "--base-url", self.stub.url, *extra)

    def test_files_through_the_launcher_route_and_prints_the_number(self) -> None:
        self.stub.routes["/api/board/issues/file"] = (
            200, {"repo": "demo", "url": "https://gitlab.example/g/demo/-/issues/12"})
        code, out = self._file()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), ["FILED=demo#12", "URL=https://gitlab.example/g/demo/-/issues/12"])
        self.assertEqual(self.stub.posts, [("/api/board/issues/file", {
            "repo": "demo", "title": "A title", "body": "## What\n\nSomething.\n"})])

    def test_a_4xx_is_refused_with_the_launchers_reason(self) -> None:
        self.stub.routes["/api/board/issues/file"] = (404, {"detail": "no project named demo"})
        code, out = self._file()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("REFUSED=the launcher refused the filing (HTTP 404)"), out)
        self.assertIn("no project named demo", out)

    def test_a_5xx_is_unconfirmed_not_refused(self) -> None:
        self.stub.routes["/api/board/issues/file"] = (502, {"detail": "glab could not file the issue"})
        code, out = self._file()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("UNCONFIRMED="), out)
        self.assertNotIn("FILED=", out)

    def test_a_reply_without_an_issue_number_is_unconfirmed_not_filed(self) -> None:
        self.stub.routes["/api/board/issues/file"] = (200, {"repo": "demo", "url": "https://gitlab.example/g/demo"})
        code, out = self._file()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("UNCONFIRMED="), out)
        self.assertNotIn("FILED=", out)

    def test_an_empty_title_or_body_is_refused_before_any_post(self) -> None:
        code, out = self._file(title="  ")
        self.assertEqual((code, out.strip()), (1, "REFUSED=title is empty"))
        self.body.write_text("\n", encoding="utf-8")
        code, out = self._file()
        self.assertEqual(code, 1)
        self.assertIn("REFUSED=body file is empty", out)
        code, out = _ops("file", "demo", "--title", "T", "--body-file", str(self.state / "missing.md"),
                         "--base-url", self.stub.url)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED=body file unreadable", out)
        self.assertEqual(self.stub.posts, [])

    def test_a_silent_launcher_is_unconfirmed(self) -> None:
        self.stub.routes["/api/board/issues/file"] = (200, {})
        with mock.patch.object(launcher, "_request", side_effect=TimeoutError("timed out")):
            code, out = self._file()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("UNCONFIRMED=no answer within"), out)

    def test_issue_number_reads_both_gitlab_url_shapes(self) -> None:
        from chief import lite_file
        self.assertEqual(lite_file.issue_number("https://h/g/p/-/issues/7"), 7)
        self.assertEqual(lite_file.issue_number("https://h/g/p/issues/8/"), 8)
        self.assertEqual(lite_file.issue_number("https://h/g/p/-/work_items/9"), 9)
        self.assertIsNone(lite_file.issue_number("https://h/g/p/-/merge_requests/10"))
        self.assertIsNone(lite_file.issue_number(None))


class GlabIssueTests(unittest.TestCase):
    def test_glab_issue_is_normalised_to_the_gh_row(self) -> None:
        row = launcher.normalise_glab_issue(
            {"iid": 5, "title": "T", "state": "opened", "labels": ["needs-owner", "bug"],
             "updated_at": "2026-11-17T08:00:00Z"}, "demo")
        self.assertEqual(row, {"repo": "demo", "number": 5, "title": "T", "state": "OPEN",
                               "labels": [{"name": "needs-owner"}, {"name": "bug"}],
                               "updatedAt": "2026-11-17T08:00:00Z"})

    def test_missing_glab_is_an_error_row_not_a_crash(self) -> None:
        with mock.patch.object(launcher, "run_glab", side_effect=FileNotFoundError):
            self.assertEqual(launcher.fetch_issue_state("demo", 5, owner="me")["error"], "glab not installed")
            code, out = _ops("issues", "demo#5", "--owner", "me")
        self.assertEqual((code, out), (0, "demo#5: error: glab not installed\n"))


def _fake_glab(states: list, returncode: int = 0):
    def run(_args):
        body = json.dumps([{"iid": 1, "state": s} for s in states])
        return subprocess.CompletedProcess(_args, returncode, stdout=body, stderr="boom")
    return run


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name) / "demo"
        self.repo.mkdir()
        # A user-level core.hooksPath (author allowlists, signing) must not reach a throwaway repo.
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                    ["config", "user.name", "t"], ["config", "core.hooksPath", str(self.repo / ".no-hooks")],
                    ["commit", "-q", "--allow-empty", "-m", "x"],
                    ["checkout", "-q", "-b", "feat/1-x"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True,
                           timeout=30, creationflags=NO_WINDOW)

    def _args(self, expect: str, **kw) -> SimpleNamespace:
        base = dict(repo=str(self.repo), expect=expect, branch=None, default_branch="main",
                    owner="me", project=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_mr_open_clean_and_missing(self) -> None:
        self.assertEqual(lite_verify.run_verify(self._args("mr-open"), _fake_glab(["opened"])),
                         ("CLEAN", "feat/1-x", None))
        status, _branch, reason = lite_verify.run_verify(self._args("mr-open"), _fake_glab(["closed"]))
        self.assertEqual(status, "DIRTY")
        self.assertIn("no open merge request from feat/1-x", reason)

    def test_merged_needs_default_branch_and_a_merged_mr(self) -> None:
        status, _b, reason = lite_verify.run_verify(self._args("merged", branch="feat/1-x"), _fake_glab(["merged"]))
        self.assertEqual((status, reason), ("DIRTY", "on feat/1-x, not back on main"))
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "main"], check=True,
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
        self.assertEqual(lite_verify.run_verify(self._args("merged", branch="feat/1-x"), _fake_glab(["merged"]))[0],
                         "CLEAN")
        status, _b, reason = lite_verify.run_verify(self._args("merged"), _fake_glab(["merged"]))
        self.assertEqual((status, reason), ("UNKNOWN", "--branch is needed to find the lane's merge request"))

    def test_unknowns_are_never_clean_or_dirty(self) -> None:
        def missing(_args):
            raise FileNotFoundError
        self.assertEqual(lite_verify.run_verify(self._args("mr-open"), missing)[::2], ("UNKNOWN", "glab not installed"))
        self.assertEqual(lite_verify.run_verify(self._args("mr-open"), _fake_glab([], returncode=1))[0], "UNKNOWN")
        self.assertEqual(lite_verify.run_verify(self._args("mr-open", repo="no-such-repo-anywhere"),
                                                _fake_glab(["opened"]))[0], "UNKNOWN")

    def test_dirty_tree_wins(self) -> None:
        (self.repo / "f.txt").write_text("x", encoding="utf-8")
        status, _b, reason = lite_verify.run_verify(self._args("mr-open"), _fake_glab(["opened"]))
        self.assertEqual((status, reason), ("DIRTY", "uncommitted changes in the checkout"))


if __name__ == "__main__":
    unittest.main()
