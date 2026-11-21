"""Tests for the ported chief helpers (stdlib unittest, hermetic state dir).

Run:  python -m unittest discover -s tests -v

Two contracts: every file docs/chief.md lists as a byte-identical port still
hashes to its recorded sha256, and the lite `chief_ops.py` writes the plan and
journal under the Copilot state directory in the shared v1 format.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))

import chief_ops  # noqa: E402
from fleet_core import hooks_state  # noqa: E402

PROVENANCE_ROW = re.compile(r"^\| `(skills/_lib/[^`]+)` \| `([0-9a-f]{64})` \|$")


def _run(*argv: str) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = chief_ops.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ProvenanceTests(unittest.TestCase):
    def test_every_listed_port_matches_its_hash(self) -> None:
        rows = [m.groups() for line in (REPO / "docs" / "chief.md").read_text(encoding="utf-8").splitlines()
                if (m := PROVENANCE_ROW.match(line))]
        self.assertGreaterEqual(len(rows), 10)
        for rel, expected in rows:
            data = (REPO / rel).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected, f"{rel} drifted from its port")


class StateDirTests(unittest.TestCase):
    def test_default_is_copilot_and_env_overrides(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(hooks_state.STATE_DIR_ENV_VAR, None)
            self.assertEqual(hooks_state.state_dir(), Path.home() / ".copilot" / "hooks" / "state")
        with mock.patch.dict(os.environ, {"COPILOT_HOOKS_STATE_DIR": "X"}):
            self.assertEqual(hooks_state.state_dir(), Path("X"))


class ChiefOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.state = Path(self._td.name)
        patcher = mock.patch.dict(os.environ, {"COPILOT_HOOKS_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._td.cleanup)

    def test_plan_round_trip_writes_v1_and_journals_each_transition(self) -> None:
        self.assertEqual(_run("plan", "add", "app-launcher-lite#5", "Do a thing")[0], 0)
        self.assertEqual(_run("plan", "lane", "fleet-config-lite", "building", "--item", "#9")[0], 0)
        code, out, _ = _run("plan", "ask", "Pick one?", "--ref", "app-launcher-lite#5",
                            "--option", "A::first", "--option", "B::second", "--recommended", "A")
        self.assertEqual(code, 0)
        self.assertIn("id=q1", out)
        self.assertEqual(_run("plan", "set", "app-launcher-lite#5", "--status", "merged")[0], 0)
        code, out, _ = _run("plan", "prune")
        self.assertTrue(out.startswith("PRUNED=1 PLAN=written"))

        plan = json.loads((self.state / "chief-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["queue"], [])
        self.assertEqual(plan["lanes"][0]["repo"], "fleet-config-lite")
        self.assertEqual(plan["waiting_on_roberto"][0]["id"], "q1")
        kinds = [json.loads(line)["kind"] for line in
                 (self.state / "chief-journal.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kinds, ["filed", "dispatched", "asked", "merged"])

    def test_note_appends_then_skips_a_duplicate(self) -> None:
        self.assertEqual(_run("note", "shipped", "--ref", "fleet-config-lite#9")[1].strip(),
                         "NOTE=appended kind=note ref=fleet-config-lite#9")
        self.assertEqual(_run("note", "shipped", "--ref", "fleet-config-lite#9")[1].strip(),
                         "NOTE=skipped reason=duplicate")

    def test_owner_kind_is_refused_without_writing_when_the_label_is_unconfirmed(self) -> None:
        from owner_check import github as forge

        with mock.patch.object(forge, "_glab", side_effect=forge.GhError("glab not installed")):
            code, out, err = _run("plan", "add", "fleet-config-lite#8", "z", "--status", "parked", "--kind", "owner")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("could not confirm fleet-config-lite#8 carries needs-owner", err)
        self.assertFalse((self.state / "chief-plan.json").exists())

    def test_invalid_write_is_refused_with_exit_2(self) -> None:
        code, _out, err = _run("plan", "set", "fleet-config-lite#404", "--status", "merged")
        self.assertEqual(code, 2)
        self.assertIn("ERROR:", err)


if __name__ == "__main__":
    unittest.main()
