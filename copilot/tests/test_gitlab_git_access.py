"""Tests for the check that git can reach GitLab with no prompt (stdlib unittest).

Run:  python -m unittest discover -s tests -v

The classifier and the remote finder run against a recording fake runner. Two cases
drive the real `git`: one against a local repository (answered) and one against a
loopback server that always answers 401 (refused for want of a credential), so the
refusal markers are checked against what git actually prints. Nothing reaches GitLab.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))

import gitlab_git_access as access  # noqa: E402
from git_run import run_git  # noqa: E402

GITLAB_URL = "https://gitlab.example.test/team/proj.git"


def _result(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


class _Fake:
    """Answers `ls-remote` with `probe`, `config --get-all` with `helpers`, origins from `origins`."""

    def __init__(self, probe=None, helpers: str = "", origins: dict | None = None) -> None:
        self.probe = probe or _result()
        self.helpers = helpers
        self.origins = origins or {}
        self.calls: list[list[str]] = []
        self.env: dict | None = None

    def __call__(self, args, *, timeout=None, extra_env=None, **_kw):
        self.calls.append(list(args))
        if "ls-remote" in args:
            self.env = extra_env
            if isinstance(self.probe, BaseException):
                raise self.probe
            return self.probe
        if "--get-all" in args:
            return _result(0 if self.helpers else 1, self.helpers)
        if "remote.origin.url" in args:
            url = self.origins.get(Path(args[1]).name)
            return _result(0, url + "\n") if url else _result(1)
        return _result(1)


class ClassifyTests(unittest.TestCase):
    def test_an_answered_probe_is_ok(self) -> None:
        self.assertEqual(access.classify(_result(0, "abc\tHEAD\n"))[0], access.OK)

    def test_each_credential_refusal_is_missing(self) -> None:
        for err in (
            "fatal: Cannot prompt because user interactivity has been disabled.\n"
            "fatal: could not read Username for 'https://gitlab.com': terminal prompts disabled",
            "remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://gitlab.com/x.git/'",
        ):
            with self.subTest(err=err[:40]):
                self.assertEqual(access.classify(_result(128, err=err))[0], access.MISSING)

    def test_a_non_auth_failure_is_unknown_not_missing(self) -> None:
        status, reason = access.classify(_result(128, err="fatal: unable to access: Could not resolve host"))
        self.assertEqual(status, access.UNKNOWN)
        self.assertIn("Could not resolve host", reason)


class CheckTests(unittest.TestCase):
    def test_missing_prints_the_helper_fix_for_the_remote_host(self) -> None:
        fake = _Fake(_result(128, err="fatal: could not read Username for 'https://gitlab.example.test'"))
        verdict = access.check(Path("repo"), GITLAB_URL, fake)
        self.assertEqual(verdict.status, access.MISSING)
        self.assertEqual(verdict.host, "gitlab.example.test")
        self.assertTrue(verdict.fix[0].endswith('credential.https://gitlab.example.test.helper ""'))
        self.assertTrue(verdict.fix[1].endswith('"!glab auth git-credential"'))

    def test_the_probe_runs_with_prompts_off_and_a_timeout(self) -> None:
        fake = _Fake()
        access.check(Path("repo"), GITLAB_URL, fake)
        self.assertEqual(fake.env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(fake.env["GCM_INTERACTIVE"], "never")

    def test_a_helper_already_wired_to_glab_points_at_sign_in_instead(self) -> None:
        fake = _Fake(_result(128, err="fatal: Authentication failed"), helpers="\n!glab auth git-credential\n")
        verdict = access.check(Path("repo"), GITLAB_URL, fake)
        self.assertEqual(verdict.status, access.MISSING)
        self.assertEqual(len(verdict.fix), 1)
        self.assertIn("glab auth login --hostname gitlab.example.test", verdict.fix[0])

    def test_a_timeout_and_a_missing_git_are_unknown(self) -> None:
        for exc in (subprocess.TimeoutExpired("git", 20), FileNotFoundError("git")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(access.check(Path("repo"), GITLAB_URL, _Fake(exc)).status, access.UNKNOWN)

    def test_a_token_in_the_remote_is_never_echoed(self) -> None:
        url = "https://oauth2:s3cret@gitlab.example.test/team/proj.git"
        verdict = access.check(Path("repo"), url, _Fake(_result(128, err="fatal: Authentication failed")))
        self.assertNotIn("s3cret", " ".join([verdict.remote, verdict.reason, *verdict.fix]))
        self.assertEqual(verdict.host, "gitlab.example.test")


class FindRemoteTests(unittest.TestCase):
    def _tree(self, names: list[str]) -> tuple[tempfile.TemporaryDirectory, Path]:
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        for name in names:
            (root / name / ".git").mkdir(parents=True)
        return td, root

    def test_no_https_gitlab_remote_anywhere_is_unknown_with_no_probe(self) -> None:
        td, root = self._tree(["lite", "a", "b"])
        self.addCleanup(td.cleanup)
        fake = _Fake(origins={"lite": "https://github.com/o/lite.git", "a": "git@gitlab.com:g/a.git"})
        verdict = access.check(root / "lite", "", fake)
        self.assertEqual(verdict.status, access.UNKNOWN)
        self.assertIn("no https GitLab remote", verdict.reason)
        self.assertFalse(any("ls-remote" in c for c in fake.calls))

    def test_a_sibling_checkout_supplies_the_remote(self) -> None:
        td, root = self._tree(["lite", "a", "b"])
        self.addCleanup(td.cleanup)
        fake = _Fake(origins={"lite": "https://github.com/o/lite.git", "b": GITLAB_URL})
        self.assertEqual(access.find_remote(root / "lite", fake), GITLAB_URL)

    def test_the_checkouts_own_gitlab_origin_wins(self) -> None:
        td, root = self._tree(["lite", "a"])
        self.addCleanup(td.cleanup)
        own = "https://gitlab.example.test/team/lite.git"
        fake = _Fake(origins={"lite": own, "a": GITLAB_URL})
        self.assertEqual(access.find_remote(root / "lite", fake), own)


class _Unauthorized(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="gitlab"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


class RealGitTests(unittest.TestCase):
    def test_a_local_repository_answers_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "r"
            repo.mkdir()
            git = ["-C", str(repo)]
            self.assertEqual(run_git([*git, "init", "-q", "-b", "main"]).returncode, 0)
            # Plumbing, not `git commit`: no hook can reject the throwaway identity.
            commit = run_git([*git, "commit-tree", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-m", "x"],
                             extra_env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.test",
                                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.test"})
            self.assertEqual(commit.returncode, 0, commit.stderr)
            self.assertEqual(run_git([*git, "update-ref", "refs/heads/main", commit.stdout.strip()]).returncode, 0)
            verdict = access.check(repo, str(repo))
            self.assertEqual(verdict.status, access.OK, verdict.reason)

    def test_a_server_that_wants_a_credential_is_missing_under_the_real_git_wording(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _Unauthorized)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        verdict = access.check(REPO, f"http://127.0.0.1:{server.server_port}/g/p.git")
        self.assertEqual(verdict.status, access.MISSING, verdict.reason)


class CliTests(unittest.TestCase):
    def test_the_cli_prints_one_status_line_and_exits_zero(self) -> None:
        out = io.StringIO()
        with mock.patch.object(access, "find_remote", return_value=""), contextlib.redirect_stdout(out):
            code = access.main(["check", "--repo", "."])
        self.assertEqual(code, 0)
        self.assertIn("GIT_ACCESS=unknown", out.getvalue().splitlines())


if __name__ == "__main__":
    unittest.main()
