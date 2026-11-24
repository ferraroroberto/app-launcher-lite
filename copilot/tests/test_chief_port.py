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


class SkillCommandTests(unittest.TestCase):
    def test_the_skill_command_finds_the_launcher_where_the_installer_writes_it(self) -> None:
        sys.path.insert(0, str(REPO))
        import install  # noqa: PLC0415 -- copilot/install.py, only importable once REPO is on sys.path

        skill = (REPO / ".github" / "skills" / "chief" / "SKILL.md").read_text(encoding="utf-8")
        home = '$(if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$HOME/.copilot" })'
        self.assertIn(f'& "{home}/hooks/{install.CHIEF_LAUNCHER[1]}"', skill)
        with mock.patch.dict(os.environ, {"COPILOT_HOME": "elsewhere"}):
            self.assertEqual(install.Paths().hooks, os.path.join("elsewhere", "hooks"))


class StateDirTests(unittest.TestCase):
    def test_default_is_copilot_and_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home:
            # Path.home() reads USERPROFILE on Windows and HOME elsewhere; point both at a fake home.
            with mock.patch.dict(os.environ, {"HOME": fake_home, "USERPROFILE": fake_home}, clear=False):
                os.environ.pop(hooks_state.STATE_DIR_ENV_VAR, None)
                current = Path(fake_home) / ".copilot" / "app-launcher-lite-state"
                legacy = Path(fake_home) / ".copilot" / "hooks" / "state"
                self.assertEqual(hooks_state.state_dir(), current)
                legacy.mkdir(parents=True)
                self.assertEqual(hooks_state.state_dir(), legacy, "unmigrated state stays where its writers left it")
                current.mkdir()
                self.assertEqual(hooks_state.state_dir(), current)
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

    def test_unwait_with_no_match_is_a_no_op_not_a_failure(self) -> None:
        self.assertEqual(_run("plan", "ask", "Pick one?", "--id", "pick")[0], 0)
        plan_path = self.state / "chief-plan.json"
        before = plan_path.read_bytes()
        code, out, err = _run("plan", "unwait", "slots-idle")
        self.assertEqual((code, out.strip(), err), (0, "UNWAIT=none id=slots-idle", ""))
        self.assertEqual(plan_path.read_bytes(), before, "a no-op unwait writes nothing")
        code, out, _ = _run("plan", "unwait", "pick")
        self.assertEqual(code, 0)
        self.assertNotIn("UNWAIT=none", out)

    def test_a_lane_can_carry_a_skill_run(self) -> None:
        code, out, _ = _run("plan", "lane", "task-os", "building", "--item", "skill:upstream-sync")
        self.assertEqual(code, 0, out)
        plan = json.loads((self.state / "chief-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["lanes"], [{"repo": "task-os", "status": "building",
                                          "item": "skill:upstream-sync"}])
        entry = json.loads((self.state / "chief-journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["kind"], "dispatched")
        self.assertIn("running upstream-sync", entry["text"])
        for bad in ("upstream-sync task-os", "skill:", "skill:Upstream Sync"):
            code, _out, err = _run("plan", "lane", "task-os", "building", "--item", bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("must be #N, skill:<name> or empty", err)


if __name__ == "__main__":
    unittest.main()
