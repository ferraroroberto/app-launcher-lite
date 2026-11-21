"""Tests for the lite sweep: the GitLab lookup and the lite-only scope (stdlib unittest).

Run:  python -m unittest discover -s tests -v

On a machine that also runs the full fleet, the sweep folder holds that fleet's
worktrees too; lite must never touch them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))

from chief import chief_sweep, lite_sweep  # noqa: E402


def _finding(name: str, cls: str = chief_sweep.FINISHED) -> chief_sweep.Finding:
    return chief_sweep.Finding(name=name, path=Path("X") / name, cls=cls, evidence="")


class ScopeTests(unittest.TestCase):
    def test_only_lite_repos_are_in_scope(self) -> None:
        named = lite_sweep.lite_repos({"sid": {"repo": "team-app"}}, {"k": {"repo": "fleet-config-lite"}, "bad": 3})
        self.assertEqual(named, {"team-app", "fleet-config-lite"})
        kept, outside = lite_sweep.split_scope(
            [_finding("app-launcher-lite-wt-4"), _finding("team-app-wt-2"), _finding("fleet-config-wt-2100")], named)
        self.assertEqual([f.name for f in kept], ["app-launcher-lite-wt-4", "team-app-wt-2"])
        self.assertEqual(outside, ["fleet-config-wt-2100"])

    def test_apply_never_reaches_an_out_of_scope_worktree(self) -> None:
        kept, _outside = lite_sweep.split_scope([_finding("fleet-config-wt-1")], set())
        removed = []
        chief_sweep.apply_findings(kept, lambda p: removed.append(p) or 0)
        self.assertEqual(removed, [])


class LookupTests(unittest.TestCase):
    def test_gitlab_rows_normalise_to_the_gh_shape(self) -> None:
        def glab(args):
            self.assertEqual(args[:3], ["mr", "list", "--repo"])
            self.assertEqual(args[3], "me/demo")
            body = json.dumps([{"iid": 4, "state": "merged", "sha": "abc"}])
            return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")
        rows = lite_sweep.gitlab_mr_lookup("https://gitlab.com/me/demo.git", "feat/4-x", glab)
        self.assertEqual(rows, [{"number": 4, "state": "MERGED", "headRefOid": "abc"}])

    def test_unknowns_are_none(self) -> None:
        def missing(_args):
            raise FileNotFoundError
        self.assertIsNone(lite_sweep.gitlab_mr_lookup("https://gitlab.com/me/demo.git", "b", missing))
        self.assertIsNone(lite_sweep.gitlab_mr_lookup("not-a-remote", "b", missing))
        failed = lambda a: subprocess.CompletedProcess(a, 1, stdout="", stderr="401")  # noqa: E731
        self.assertIsNone(lite_sweep.gitlab_mr_lookup("git@gitlab.com:me/demo.git", "b", failed))

    def test_host_dispatch(self) -> None:
        with mock.patch.object(chief_sweep, "github_pr_lookup", return_value=["gh"]) as gh, \
                mock.patch.object(lite_sweep, "gitlab_mr_lookup", return_value=["glab"]) as gl:
            self.assertEqual(lite_sweep.forge_lookup("https://github.com/me/demo", "b"), ["gh"])
            self.assertEqual(lite_sweep.forge_lookup("https://gitlab.com/me/demo", "b"), ["glab"])
        gh.assert_called_once()
        gl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
