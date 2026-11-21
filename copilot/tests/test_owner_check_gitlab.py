"""Tests for the GitLab owner check and the lite drain intake (stdlib unittest).

Run:  python -m unittest discover -s tests -v

`owner_check/github.py` is lite's GitLab seam behind the verbatim owner-check
CLI; `_glab` is replaced by a scripted fake, so no network is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))
sys.path.insert(0, str(REPO / "hooks"))

import chief_guard  # noqa: E402
import gitlab_namespace  # noqa: E402
from chief import drain  # noqa: E402
from owner_check import block, cli  # noqa: E402
from owner_check import github as forge  # noqa: E402


def setUpModule() -> None:
    """The namespace comes from the live launcher; pin it so no test reads the real one."""
    patcher = mock.patch.object(gitlab_namespace, "from_config", return_value=("ferraroroberto", ""))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeGlab:
    """Answers glab argv by prefix; records every call."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls: list = []

    def __call__(self, args) -> str:
        self.calls.append(list(args))
        for key, value in self.answers.items():
            if " ".join(args).startswith(key):
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, str) else json.dumps(value)
        raise forge.GhError(f"unscripted: {' '.join(args)}")


def _issue(labels) -> dict:
    return {"iid": 5, "title": "T", "description": "body", "state": "opened",
            "web_url": "https://gitlab.com/ferraroroberto/demo/-/issues/5", "labels": labels}


BLOCK = "\n".join([block.MARKER, "**Owner check: before start**", "", "- kind: before-start",
                   "- reason: needs a token", "- try: create it", "- since: 2026-11-17"])


class ForgeTests(unittest.TestCase):
    def test_scope_is_opt_in_and_read_back_plain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            toml = Path(td) / ".fleet.toml"
            self.assertEqual(forge.label_scope(toml), "")
            toml.write_text('[owner_check]\nlabel_scope = "owner"\n', encoding="utf-8")
            self.assertEqual(forge.label_scope(toml), "owner")
        with mock.patch.object(forge, "label_scope", return_value="owner"):
            self.assertEqual(forge.forge_label("needs-owner"), "owner::needs-owner")
        self.assertEqual(forge.plain_label("owner::needs-owner"), "needs-owner")
        self.assertEqual(forge.plain_label("priority::high"), "priority::high")

    def test_fetch_issue_maps_gitlab_to_the_callers_shape(self) -> None:
        fake = FakeGlab({"issue view": _issue(["owner::needs-owner", "bug"]),
                         "api projects/ferraroroberto%2Fdemo/issues/5/closed_by": [{"web_url": "mr-url"}],
                         "api projects/ferraroroberto%2Fdemo/issues/5/notes": [
                             {"body": "hi", "system": False}, {"body": "label added", "system": True}]})
        with mock.patch.object(forge, "_glab", fake):
            issue = forge.fetch_issue("demo", 5)
        self.assertEqual(issue["labels"], ["needs-owner", "bug"])
        self.assertEqual((issue["state"], issue["comments"], issue["prs"]), ("OPEN", ["hi"], ["mr-url"]))

    def test_mark_writes_the_scoped_label_after_the_note(self) -> None:
        fake = FakeGlab({"label create": forge.GhError("Label already exists"), "issue note": "", "issue update": ""})
        with mock.patch.object(forge, "_glab", fake), mock.patch.object(forge, "label_scope", return_value="owner"):
            self.assertEqual(forge.mark("demo", 5, block.BEFORE_START, BLOCK), "needs-owner")
        self.assertEqual([c[1] for c in fake.calls], ["create", "note", "update"])
        self.assertEqual(fake.calls[2][-2:], ["--label", "owner::needs-owner"])

    def test_cli_gate_blocks_and_unknowns(self) -> None:
        fake = FakeGlab({"issue view": _issue(["needs-owner"]), "api projects/ferraroroberto%2Fdemo/issues/5/closed_by": [],
                         "api projects/ferraroroberto%2Fdemo/issues/5/notes": [{"body": BLOCK}]})
        out = io.StringIO()
        with mock.patch.object(forge, "_glab", fake), contextlib.redirect_stdout(out):
            code = cli.main(["gate", "demo#5"])
        self.assertNotEqual(code, 0)
        self.assertIn("OWNER_GATE=blocked", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(forge, "_glab", FakeGlab({"issue view": forge.GhError("glab not installed")})), \
                contextlib.redirect_stdout(out):
            cli.main(["gate", "demo#5"])
        self.assertIn("OWNER_GATE=unknown", out.getvalue())

    def test_list_reports_a_failing_repo_as_unknown(self) -> None:
        fake = FakeGlab({"issue list": forge.GhError("401 unauthorized")})
        with mock.patch.object(forge, "_glab", fake):
            result = forge.list_items(["demo"])
        self.assertEqual(result, {"items": [], "unknown": [{"repo": "demo", "reason": "401 unauthorized"}]})

    def test_merge_requests_are_never_issues_on_gitlab(self) -> None:
        self.assertFalse(forge.is_pull_request("demo", 5))
        self.assertEqual(forge.pr_url("demo", 7), "https://gitlab.com/ferraroroberto/demo/-/merge_requests/7")


class DrainIntakeTests(unittest.TestCase):
    def test_gitlab_rows_feed_the_verbatim_intake(self) -> None:
        body = "<!-- audit-managed: kind=slop -->\n- [ ] remove the dead helper"
        rows = [{"iid": 3, "title": "audit: slop findings", "description": body, "labels": []},
                {"iid": 4, "title": "x", "description": body, "labels": ["owner::needs-owner"]},
                {"iid": 6, "title": "plain", "description": "no marker", "labels": []}]
        with mock.patch.object(forge, "_glab", FakeGlab({"issue list": rows})):
            issues = drain._repo_issues("demo")
        adds, skips = drain.select_intake(issues, {"lanes": [], "queue": [], "waiting_on_roberto": []}, set(), set())
        self.assertEqual([a["ref"] for a in adds], ["demo#3"])
        self.assertEqual(skips, [("demo#4", "label-needs-owner")])

    def test_a_repo_glab_cannot_read_is_an_error_row(self) -> None:
        with mock.patch.object(forge, "fleet_repo_names", return_value=["demo"]), \
                mock.patch.object(forge, "_glab", FakeGlab({"issue list": forge.GhError("glab not installed")})):
            self.assertEqual(drain.fetch_open_issues(), ([], [("demo", "glab not installed")]))


class GuardSlotsTests(unittest.TestCase):
    def test_an_off_switch_never_holds(self) -> None:
        with mock.patch.object(chief_guard, "chief_ops", return_value="SLOTS_IDLE=off reason=auto-drain-off\n"):
            self.assertIsNone(chief_guard.slots_hold(0.0))


if __name__ == "__main__":
    unittest.main()
