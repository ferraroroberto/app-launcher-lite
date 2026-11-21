"""Tests for skills/_lib/issue_host.py -- URL parsing, marker, gh/glab argv shapes.

Run:  python -m unittest discover -s tests -v

Pure helpers only: no gh, glab or git is spawned.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "_lib"))

import issue_host as ih  # noqa: E402


class RemoteUrlTests(unittest.TestCase):
    def test_owner_repo_across_url_shapes(self):
        cases = {
            "https://github.com/o/r.git": "o/r",
            "git@github.com:o/r.git": "o/r",
            "ssh://git@gitlab.example.com/grp/sub/proj.git": "grp/sub/proj",
            "https://gitlab.example.com/group/subgroup/project.git": "group/subgroup/project",
            "git@gitlab.com:group/project.git": "group/project",
            "": "",
            "nonsense": "",
            "https://github.com/lonely": "",
        }
        for url, want in cases.items():
            self.assertEqual(ih.parse_owner_repo(url), want, url)

    def test_split_keeps_subgroups_in_the_owner(self):
        self.assertEqual(ih.split_owner_repo("group/subgroup/project"), ("group/subgroup", "project"))
        self.assertEqual(ih.split_owner_repo("o/r"), ("o", "r"))
        self.assertEqual(ih.split_owner_repo(""), ("", ""))

    def test_host_and_cli(self):
        self.assertEqual(ih.detect_host("https://github.com/a/b.git"), "github")
        self.assertEqual(ih.detect_host("https://gitlab.com/a/b.git"), "gitlab")
        self.assertEqual(ih.detect_host("git@gitlab.corp.internal:a/b.git"), "gitlab")
        self.assertEqual(ih.detect_host(""), "gitlab")
        self.assertEqual((ih.cli_for("github"), ih.cli_for("gitlab")), ("gh", "glab"))


class MarkerTests(unittest.TestCase):
    def test_marker_only_on_first_line(self):
        self.assertTrue(ih.has_marker("<!-- audit-managed: kind=slop -->\n\nbody", "slop"))
        self.assertFalse(ih.has_marker("prose quoting <!-- audit-managed: kind=slop -->", "slop"))
        self.assertFalse(ih.has_marker("<!-- audit-managed: kind=slop -->", "bug"))

    def test_ensure_marker_is_idempotent(self):
        once = ih.ensure_marker("hello", "bug")
        self.assertEqual(ih.ensure_marker(once, "bug"), once)
        self.assertEqual(ih.ensure_marker("", "bug"), "<!-- audit-managed: kind=bug -->\n")


class IssueArgvTests(unittest.TestCase):
    def test_list_args_filter_by_label_and_state(self):
        gh = ih.list_issues_args("github", "o/r", "audit-meta")
        self.assertEqual(gh[gh.index("--state") + 1], "all")
        self.assertEqual(gh[gh.index("--label") + 1], "audit-meta")
        gh_open = ih.list_issues_args("github", "o/r", "x", state="open")
        self.assertEqual(gh_open[gh_open.index("--state") + 1], "open")
        glab_all = ih.list_issues_args("gitlab", "g/r", "audit-meta")
        self.assertIn("--all", glab_all)
        self.assertNotIn("--all", ih.list_issues_args("gitlab", "g/r", "audit-meta", state="open"))

    def test_normalise_github_lowercases_state(self):
        out = ih.normalise_issues("github", [{"number": 3, "state": "OPEN", "body": "b"}])
        self.assertEqual(out[0]["state"], "open")

    def test_normalise_gitlab_maps_iid_description_and_state(self):
        out = ih.normalise_issues("gitlab", [
            {"iid": 4, "title": "t", "description": None, "state": "opened", "web_url": "u"},
            {"iid": 5, "state": "closed"},
        ])
        self.assertEqual(out[0], {"number": 4, "title": "t", "body": "", "state": "open", "url": "u"})
        self.assertEqual((out[1]["number"], out[1]["state"]), (5, "closed"))
        self.assertEqual(ih.normalise_issues("gitlab", {"not": "a list"}), [])

    def test_create_edit_comment_use_each_hosts_flags(self):
        self.assertIn("--body-file", ih.create_issue_args("github", "o/r", "T", "f.md", "L"))
        glab_create = ih.create_issue_args("gitlab", "g/r", "T", "f.md", "L")
        self.assertIn("--description-file", glab_create)
        self.assertIn("--yes", glab_create)
        self.assertEqual(ih.edit_issue_args("github", "o/r", 7, "T", "f.md", "L")[:3], ["issue", "edit", "7"])
        self.assertEqual(ih.edit_issue_args("gitlab", "g/r", 7, "T", "f.md", "L")[:3], ["issue", "update", "7"])
        self.assertEqual(ih.comment_args("github", "o/r", 7, "f.md")[:2], ["issue", "comment"])
        self.assertEqual(ih.comment_args("gitlab", "g/r", 7, "f.md")[:2], ["issue", "note"])

    def test_label_create_args(self):
        self.assertEqual(ih.label_create_args("github", "o/r", "L"), ["label", "create", "L", "--repo", "o/r"])
        self.assertEqual(ih.label_create_args("gitlab", "g/r", "L"), ["label", "create", "--repo", "g/r", "-n", "L"])
        gl = ih.label_create_args("gitlab", "g/r", "L", color="0e8a16", description="d")
        self.assertEqual(gl[gl.index("--color") + 1], "#0e8a16")
        self.assertEqual(ih.label_create_args("github", "o/r", "L", color="0e8a16")[-1], "0e8a16")

    def test_view_url_round_trip(self):
        self.assertEqual(ih.url_from_view("github", {"url": "u"}), "u")
        self.assertEqual(ih.url_from_view("gitlab", {"web_url": "w"}), "w")
        self.assertIsNone(ih.url_from_view("github", []))
        self.assertEqual(ih.created_issue_url("noise\nhttps://x/issues/9"), "https://x/issues/9")
        self.assertEqual(ih.created_issue_url(""), "")


if __name__ == "__main__":
    unittest.main()
