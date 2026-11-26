"""Tests for lite_config: every Copilot-side reader of `webapp_config.json` agrees (stdlib unittest).

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
for folder in (REPO, REPO / "hooks", REPO / "skills" / "_lib"):
    sys.path.insert(0, str(folder))

import chief_guard  # noqa: E402
import install  # noqa: E402
import lite_config  # noqa: E402
import lite_repos  # noqa: E402

from chief import lite_commands  # noqa: E402


class ReadersAgreeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.config = self.tmp / "webapp_config.json"
        for patch in (mock.patch.object(lite_config, "WEBAPP_CONFIG", self.config),
                      mock.patch.object(install, "WEBAPP_CONFIG", str(self.config))):
            patch.start()
            self.addCleanup(patch.stop)
        self.installer = install.Installer(argparse.Namespace(what_if=True, yes=False))

    def test_a_bom_prefixed_file_is_honoured_by_every_reader(self) -> None:
        own = self.tmp / "own"
        own.mkdir()
        self.config.write_text(json.dumps({"skills_dir": str(own), "projects_dir": str(own),
                                           "chief_hold_minutes": 25, "chief_reoffer_minutes": 30}),
                               encoding="utf-8-sig")
        self.assertEqual(self.installer.skills_dir_setting(), {"state": "set", "path": str(own)})
        self.assertEqual(chief_guard.skill_dir_for("issue-start"), own / "issue-start")
        self.assertEqual(chief_guard.hold_window_s(), 1500)
        self.assertEqual(lite_commands.reoffer_seconds(), 1800.0)
        self.assertEqual(lite_repos.repos_root(), own)

    def test_a_relative_folder_is_used_by_no_reader(self) -> None:
        for value in ("my-skills", 5):
            self.config.write_text(json.dumps({"skills_dir": value}), encoding="utf-8")
            self.assertEqual(self.installer.skills_dir_setting()["state"], "unknown", value)
            self.assertEqual(chief_guard.skill_dir_for("issue-start"), chief_guard.REPO / "skills" / "issue-start")
            self.assertIsNone(lite_config.folder("skills_dir"))

    def test_a_home_relative_folder_is_expanded_by_every_reader(self) -> None:
        self.config.write_text(json.dumps({"skills_dir": "~/own-skills"}), encoding="utf-8")
        expected = Path("~/own-skills").expanduser()  # host-identity-exempt: expansion is what is under test
        self.assertEqual(self.installer.skills_dir_setting(), {"state": "set", "path": str(expected)})
        self.assertEqual(chief_guard.skill_dir_for("issue-start"), expected / "issue-start")


if __name__ == "__main__":
    unittest.main()
