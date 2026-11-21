"""Tests for the GitLab host every chief glab call is pointed at (stdlib unittest).

Run:  python -m unittest discover -s tests -v

glab sends a bare `group/project` to its default host, so a self-hosted instance 404s
unless the call names the host. The host comes from the launcher's `gitlab_host`
(a loopback stub of `GET /api/config`), else the checkout's `origin`, else gitlab.com,
and reaches glab as `GITLAB_HOST`. glab itself is replaced by a recording fake; no
test touches a real GitLab, and the hosts here are placeholders.
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
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))
sys.path.insert(0, str(REPO / "tests"))

import gitlab_namespace  # noqa: E402
import issue_host  # noqa: E402
import test_gitlab_namespace as base  # noqa: E402
from owner_check import github as forge  # noqa: E402

from chief import launcher, lite_merge, lite_sweep, lite_verify  # noqa: E402

HOST = "git.example.internal"
OTHER = "code.example.test"
_REAL_RUN = subprocess.run


class ParseHostTests(unittest.TestCase):
    def test_remote_shapes(self) -> None:
        cases = {
            f"https://{HOST}/grp/demo.git": HOST,
            f"https://user@{HOST}:8443/grp/sub/demo.git": f"{HOST}:8443",
            f"http://{HOST}/grp/demo": f"http://{HOST}",
            f"ssh://git@{HOST}:2222/grp/demo.git": HOST,
            f"git@{HOST}:grp/demo.git": HOST,
            "": "",
            "not a remote": "",
        }
        for url, host in cases.items():
            with self.subTest(url=url):
                self.assertEqual(issue_host.parse_host(url), host)

    def test_a_github_remote_is_not_a_gitlab_host(self) -> None:
        self.assertEqual(gitlab_namespace.host_from_remote("https://github.com/o/r.git"), "")
        self.assertEqual(gitlab_namespace.host_from_remote("git@github.com:o/r.git"), "")


def _repo_with_origin(parent: Path, url: str = "") -> Path:
    repo = parent / "demo"
    repo.mkdir()
    cmds = [["init", "-q", "-b", "main"]] + ([["remote", "add", "origin", url]] if url else [])
    for cmd in cmds:
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True, timeout=30,
                       creationflags=base.NO_WINDOW)
    return repo


class ResolveHostTests(base._LauncherCase):
    def _config(self, host: str) -> None:
        self.stub.routes["/api/config"] = (200, {"gitlab_group": base.GROUP, "gitlab_host": host})

    def test_the_setting_wins_over_the_origin(self) -> None:
        self._config(f"{HOST}/")
        with tempfile.TemporaryDirectory() as td:
            repo = _repo_with_origin(Path(td), f"https://{OTHER}/grp/demo.git")
            self.assertEqual(gitlab_namespace.resolve_host(repo), HOST)

    def test_an_empty_setting_takes_the_origins_host(self) -> None:
        self._config("")
        with tempfile.TemporaryDirectory() as td:
            repo = _repo_with_origin(Path(td), f"git@{HOST}:grp/demo.git")
            self.assertEqual(gitlab_namespace.resolve_host(repo), HOST)

    def test_an_unreadable_launcher_still_takes_the_origins_host(self) -> None:
        self.stub.routes.pop("/api/config")
        with tempfile.TemporaryDirectory() as td:
            repo = _repo_with_origin(Path(td), f"https://{HOST}/grp/demo.git")
            self.assertEqual(gitlab_namespace.resolve_host(repo), HOST)

    def test_no_usable_origin_falls_back_to_gitlab_com(self) -> None:
        self._config("")
        for url in ("", "https://github.com/o/demo.git"):
            with self.subTest(origin=url), tempfile.TemporaryDirectory() as td:
                self.assertEqual(gitlab_namespace.resolve_host(_repo_with_origin(Path(td), url)), "gitlab.com")
        self.assertEqual(gitlab_namespace.resolve_host(), "gitlab.com")

    def test_a_remote_the_caller_holds_is_used_without_a_checkout(self) -> None:
        self._config("")
        self.assertEqual(gitlab_namespace.resolve_host(remote_url=f"https://{HOST}/grp/demo.git"), HOST)


class _GlabCase(base._LauncherCase):
    """A launcher stub naming `HOST`, and a fake glab that records each spawn's argv and environment."""

    def setUp(self) -> None:
        super().setUp()
        self.stub.routes["/api/config"] = (200, {"gitlab_group": base.GROUP, "gitlab_host": HOST})
        self.spawns: list = []
        self.answers = {"stdout": "[]", "stderr": "", "returncode": 0}

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] != "glab":
                return _REAL_RUN(cmd, *args, **kwargs)
            self.spawns.append((list(cmd), kwargs.get("env")))
            return subprocess.CompletedProcess(cmd, self.answers["returncode"],
                                               stdout=self.answers["stdout"], stderr=self.answers["stderr"])

        patcher = mock.patch.object(subprocess, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertHost(self, host: str = HOST) -> None:
        self.assertTrue(self.spawns, "glab was never spawned")
        for cmd, env in self.spawns:
            self.assertEqual(env["GITLAB_HOST"], host, cmd)


class GlabCallTests(_GlabCase):
    """Every chief glab verb carries the host in the child environment."""

    def test_issue_state_reads_the_host(self) -> None:
        self.answers["stdout"] = json.dumps({"iid": 5, "title": "T", "state": "opened", "labels": []})
        self.assertEqual(launcher.fetch_issue_state(base.REPO_NAME, 5)["state"], "OPEN")
        self.assertHost()
        argv = self.spawns[0][0]
        self.assertEqual(argv[argv.index("--repo") + 1], f"{base.GROUP}/{base.REPO_NAME}")

    def test_verify_reads_the_host_from_the_origin_when_unset(self) -> None:
        self.stub.routes["/api/config"] = (200, {"gitlab_group": base.GROUP, "gitlab_host": ""})
        with tempfile.TemporaryDirectory() as td:
            repo = _repo_with_origin(Path(td), f"https://{OTHER}/grp/demo.git")
            args = SimpleNamespace(repo=str(repo), expect="mr-open", branch="feat/1-x", default_branch="main",
                                   owner="me", project=None)
            lite_verify.run_verify(args)
        self.assertHost(OTHER)

    def test_verify_reads_the_host_setting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(repo=str(_repo_with_origin(Path(td))), expect="mr-open", branch="feat/1-x",
                                   default_branch="main", owner="me", project=None)
            lite_verify.run_verify(args)
        self.assertHost()

    def test_merge_reads_the_host(self) -> None:
        self.answers["stdout"] = json.dumps({"iid": 7, "state": "merged"})
        args = SimpleNamespace(ref=f"{base.REPO_NAME}#12", branch="feat/12-x", ask=False, owner=None,
                               project=None, plan_file=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lite_merge.cmd_merge(args), 0)
        self.assertHost()

    def test_mr_create_reads_the_host(self) -> None:
        self.answers["stdout"] = f"https://{HOST}/grp/demo/-/merge_requests/3"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lite_merge.cmd_mr_create(SimpleNamespace(number=3, timeout=5.0)), 0)
        self.assertHost()

    def test_sweep_lookup_reads_the_host(self) -> None:
        self.assertEqual(lite_sweep.gitlab_mr_lookup(f"git@{HOST}:grp/demo.git", "feat/1-x"), [])
        self.assertHost()

    def test_sweep_lookup_falls_back_to_the_remotes_host(self) -> None:
        self.stub.routes["/api/config"] = (200, {"gitlab_group": base.GROUP, "gitlab_host": ""})
        self.assertEqual(lite_sweep.gitlab_mr_lookup(f"https://{OTHER}/grp/demo.git", "feat/1-x"), [])
        self.assertHost(OTHER)

    def test_owner_check_reads_the_host(self) -> None:
        self.answers["stdout"] = "{}"
        forge._glab(["issue", "view", "5", "--repo", "grp/demo", "--output", "json"])
        self.assertHost()
        self.assertEqual(forge.pr_url("demo", 7), f"https://{HOST}/{base.GROUP}/demo/-/merge_requests/7")

    def test_only_the_host_is_added_to_the_environment(self) -> None:
        self.answers["stdout"] = "{}"
        forge._glab(["issue", "view", "5", "--repo", "grp/demo"])
        added = set(self.spawns[0][1]) - set(os.environ)
        self.assertEqual(added, set() if "GITLAB_HOST" in os.environ else {"GITLAB_HOST"})

    def test_the_host_code_names_no_token_variable(self) -> None:
        source = (REPO / "skills" / "_lib" / "gitlab_namespace.py").read_text(encoding="utf-8").upper()
        for name in ("GITLAB_TOKEN", "PRIVATE-TOKEN", "GLAB_TOKEN"):
            self.assertNotIn(name, source)


class FailureNamesTheTargetTests(_GlabCase):
    """A 404 reads off one line: the host and project glab was asked."""

    def setUp(self) -> None:
        super().setUp()
        self.answers.update(returncode=1, stderr="ERROR 404 Not Found")
        self.target = f"host {HOST}, project me/demo"

    def test_issue_state(self) -> None:
        error = launcher.fetch_issue_state("demo", 5, owner="me")["error"]
        self.assertIn(f"tried {self.target}", error)

    def test_verify(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(repo=str(_repo_with_origin(Path(td))), expect="mr-open", branch="feat/1-x",
                                   default_branch="main", owner="me", project=None)
            status, _branch, reason = lite_verify.run_verify(args)
        self.assertEqual(status, "UNKNOWN")
        self.assertIn(f"glab mr list failed: ERROR 404 Not Found (tried {self.target})", reason)

    def test_merge(self) -> None:
        args = SimpleNamespace(ref="demo#12", branch="feat/12-x", ask=False, owner="me", project=None,
                               plan_file=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lite_merge.cmd_merge(args), 1)
        self.assertIn(f"tried {self.target}", out.getvalue())

    def test_owner_check(self) -> None:
        with self.assertRaises(forge.GhError) as caught:
            forge._glab(["issue", "view", "5", "--repo", "me/demo"])
        self.assertIn(f"tried {self.target}", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
