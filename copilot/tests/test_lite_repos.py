"""Tests for lite_repos: the folder the chief reads repos from (stdlib unittest).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))

import lite_repos  # noqa: E402

from chief import chief_sweep, launcher, lite_sweep  # noqa: E402


class ReposRootTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.config = self.tmp / "webapp_config.json"
        self.lite = self.tmp / "checkouts" / "app-launcher-lite"
        self.lite.mkdir(parents=True)

    def _write(self, body: object) -> None:
        self.config.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")

    def test_projects_dir_wins_when_it_is_a_folder(self) -> None:
        projects = self.tmp / "projects"
        projects.mkdir()
        self._write({"projects_dir": str(projects)})
        self.assertEqual(lite_repos.repos_root(self.config, self.lite), projects)

    def test_falls_back_to_the_folder_beside_the_checkout(self) -> None:
        beside = self.lite.parent
        for body in ({}, {"projects_dir": ""}, {"projects_dir": "  "}, {"projects_dir": 5},
                     {"projects_dir": str(self.tmp / "missing")}, "not json", "[]"):
            self._write(body)
            self.assertEqual(lite_repos.repos_root(self.config, self.lite), beside, body)

    def test_a_missing_config_falls_back_to_the_folder_beside_the_checkout(self) -> None:
        self.assertEqual(lite_repos.repos_root(self.tmp / "absent.json", self.lite), self.lite.parent)

    def test_the_default_is_never_the_checkout_itself(self) -> None:
        # copilot/ adds a path level, so a parents[N] count from a chief module lands in the checkout.
        self.assertNotEqual(lite_repos.repos_root(self.tmp / "absent.json"), lite_repos.LITE_ROOT)
        self.assertEqual(lite_repos.LITE_ROOT, REPO.parent)

    def test_the_launcher_and_the_default_sweep_root_use_it(self) -> None:
        self.assertEqual(launcher.REPOS_ROOT, lite_repos.repos_root())
        args = argparse.Namespace(root=None, apply=False, base_url="http://127.0.0.1:1")
        with mock.patch.object(chief_sweep, "scan", return_value=[]) as scan,                 mock.patch.object(launcher, "_request", side_effect=OSError("down")),                 mock.patch.object(lite_sweep.active_issue, "read_rows", return_value={}),                 mock.patch.object(lite_sweep.chief_managed, "state_file", return_value=Path("none")):
            lite_sweep.cmd_sweep(args)
        self.assertEqual(scan.call_args.args[0], launcher.REPOS_ROOT)


if __name__ == "__main__":
    unittest.main()
