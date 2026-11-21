"""Tests for the GitLab namespace the chief addresses every forge read to (stdlib unittest).

Run:  python -m unittest discover -s tests -v

The namespace is app-launcher-lite's `gitlab_group`, read from a loopback stub of
`GET /api/config`; glab is replaced by a recording fake. No test reads the real
launcher or GitLab.
"""

from __future__ import annotations

import contextlib
import io
import json
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
from chief import launcher, lite_merge, lite_verify  # noqa: E402
from owner_check import github as forge  # noqa: E402
from test_chief_lanes import NO_WINDOW, StubLauncher, _ops  # noqa: E402

GROUP = "team-group"
# A repo name no sibling checkout carries, so only the launcher can name its namespace.
REPO_NAME = "zz-no-such-checkout"


def _issue_glab(calls: list):
    def run(args):
        calls.append(list(args))
        body = json.dumps({"iid": 5, "title": "T", "state": "opened", "labels": ["needs-owner"]})
        return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")
    return run


class _LauncherCase(unittest.TestCase):
    """A stub launcher serving `/api/config`, wired in as the default launcher URL."""

    def setUp(self) -> None:
        self.stub = StubLauncher()
        self.addCleanup(self.stub.close)
        self.stub.routes["/api/config"] = (200, {"gitlab_group": GROUP})
        patcher = mock.patch.object(gitlab_namespace, "DEFAULT_BASE_URL", self.stub.url)
        patcher.start()
        self.addCleanup(patcher.stop)


class ResolveTests(_LauncherCase):
    def test_the_launchers_gitlab_group_is_the_namespace(self) -> None:
        self.assertEqual(gitlab_namespace.resolve(), (GROUP, ""))

    def test_an_empty_group_falls_back_to_the_checkouts_origin(self) -> None:
        self.stub.routes["/api/config"] = (200, {"gitlab_group": ""})
        with tempfile.TemporaryDirectory() as td:
            for cmd in (["init", "-q"], ["remote", "add", "origin", "https://gitlab.com/outer/inner/demo.git"]):
                subprocess.run(["git", "-C", td, *cmd], check=True, capture_output=True, timeout=30,
                               creationflags=NO_WINDOW)
            self.assertEqual(gitlab_namespace.resolve(Path(td)), ("outer/inner", ""))

    def test_nothing_naming_a_namespace_is_unknown_with_a_reason(self) -> None:
        self.stub.routes["/api/config"] = (404, {})
        namespace, why = gitlab_namespace.resolve(Path(tempfile.gettempdir()) / REPO_NAME)
        self.assertIsNone(namespace)
        self.assertIn("HTTP 404", why)

    def test_a_non_loopback_launcher_is_refused(self) -> None:
        self.assertIsNone(gitlab_namespace.from_config("http://example.com")[0])


class ChiefReadTests(_LauncherCase):
    def test_issues_without_owner_reads_the_configured_group(self) -> None:
        calls: list = []
        with mock.patch.object(launcher, "run_glab", _issue_glab(calls)):
            code, out = _ops("issues", f"{REPO_NAME}#5")
        self.assertEqual(code, 0, out)
        self.assertIn("OPEN", out)
        self.assertEqual(calls[0][calls[0].index("--repo") + 1], f"{GROUP}/{REPO_NAME}")

    def test_an_explicit_owner_still_wins(self) -> None:
        calls: list = []
        with mock.patch.object(launcher, "run_glab", _issue_glab(calls)):
            _ops("issues", f"{REPO_NAME}#5", "--owner", "other")
        self.assertEqual(calls[0][calls[0].index("--repo") + 1], f"other/{REPO_NAME}")

    def test_an_unresolvable_namespace_is_owner_unknown_never_a_glab_call(self) -> None:
        self.stub.routes["/api/config"] = (200, {"gitlab_group": ""})
        run = mock.Mock()
        with mock.patch.object(launcher, "run_glab", run):
            code, out = _ops("issues", f"{REPO_NAME}#5")
        self.assertEqual(code, 1)
        self.assertIn("OWNER=unknown", out)
        self.assertNotIn("404", out)
        run.assert_not_called()

    def test_merge_without_owner_targets_the_configured_group(self) -> None:
        calls: list = []

        def glab(args):
            calls.append(list(args))
            body = json.dumps({"iid": 7, "state": "merged"})
            return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")

        args = SimpleNamespace(ref=f"{REPO_NAME}#12", branch="feat/12-x", ask=False, owner=None,
                               project=None, plan_file=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lite_merge.cmd_merge(args, glab), 0)
        self.assertTrue(all(c[c.index("--repo") + 1] == f"{GROUP}/{REPO_NAME}" for c in calls), calls)

    def test_merge_with_an_unresolvable_namespace_is_unknown(self) -> None:
        self.stub.routes["/api/config"] = (200, {"gitlab_group": ""})
        args = SimpleNamespace(ref=f"{REPO_NAME}#12", branch="feat/12-x", ask=False, owner=None,
                               project=None, plan_file=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lite_merge.cmd_merge(args, mock.Mock()), 1)
        self.assertIn("MERGE=unknown reason=OWNER=unknown", out.getvalue())

    def test_verify_without_owner_targets_the_configured_group(self) -> None:
        calls: list = []

        def glab(args):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([{"iid": 1, "state": "opened"}]),
                                               stderr="")

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "demo"
            repo.mkdir()
            for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                        ["config", "user.name", "t"], ["config", "core.hooksPath", str(repo / ".no-hooks")],
                        ["commit", "-q", "--allow-empty", "-m", "x"], ["checkout", "-q", "-b", "feat/1-x"]):
                subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True, timeout=30,
                               creationflags=NO_WINDOW)
            args = SimpleNamespace(repo=str(repo), expect="mr-open", branch=None, default_branch="main",
                                   owner=None, project=None)
            status, _branch, _reason = lite_verify.run_verify(args, glab)
        self.assertEqual(status, "CLEAN")
        self.assertEqual(calls[0][calls[0].index("--repo") + 1], f"{GROUP}/demo")


class OwnerCheckSeamTests(_LauncherCase):
    def test_the_forge_slug_uses_the_configured_group(self) -> None:
        self.assertEqual(forge._slug(REPO_NAME), f"{GROUP}/{REPO_NAME}")

    def test_an_unresolvable_namespace_raises_the_unknown_error(self) -> None:
        self.stub.routes["/api/config"] = (200, {"gitlab_group": ""})
        with self.assertRaisesRegex(forge.GhError, "OWNER=unknown"):
            forge._slug(REPO_NAME)


if __name__ == "__main__":
    unittest.main()
