"""Tests for `chief_ops.py doctor` (stdlib unittest).

Run:  python -m unittest discover -s tests -v

Each test builds a fake checkout and Copilot home in a temp folder, so nothing
reads or writes the machine's real install; git, the Copilot CLI, the launcher
and the verb children are stubbed.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))
sys.path.insert(0, str(REPO))

import install  # noqa: E402

from chief import doctor, doctor_logs  # noqa: E402

VERB_OUTPUT = {
    "wake": "WAKE=doorbell doorbell=on fallback=waiter seconds=1800",
    "inbox": "PENDING=0",
    "board": "{}",
    "sessions": "{}",
    "chief-sid": "CHIEF_SID=abc",
    "plan": "{}",
    "slots-idle": "SLOTS_IDLE=3",
    "context": "CONTEXT sid=abc percent=6",
    "compact-due": "COMPACT_NOT_DUE percent=6 threshold=30",
    "sweep": "SUMMARY finished=0",
    "issues": "lite#1: CLOSED",
}


def fake_runner(argv, timeout):
    if argv[0].endswith("copilot"):
        return 0, "GitHub Copilot CLI 1.0.0\n"
    if argv[0].endswith(".cmd"):
        return 0, "CHIEF_SID=abc\n"
    return 0, VERB_OUTPUT[argv[2]] + "\n"


def fake_git(args, **_kwargs):
    out = "abc1234\n" if "rev-parse" in args else "0\n"
    return subprocess.CompletedProcess(args, 0, out, "")


class Machine:
    """A healthy fake install: checkout/ with copilot/, and home/ as the Copilot home."""

    def __init__(self, root: Path) -> None:
        self.checkout = root / "lite"
        self.copilot = self.checkout / "copilot"
        self.home = root / "copilot-home"
        self.profile = root / "profile"
        (self.copilot / "hooks").mkdir(parents=True)
        for template, _file, script in install.HOOK_CONFIGS:
            shutil.copy(REPO / "hooks" / template, self.copilot / "hooks" / template)
            (self.copilot / "hooks" / script).write_text("", encoding="utf-8")
        shutil.copy(REPO / "hooks" / install.CHIEF_LAUNCHER[0], self.copilot / "hooks" / install.CHIEF_LAUNCHER[0])
        self.skill = self.copilot / "skills" / "quick"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("quick\n", encoding="utf-8")
        self.python = self.checkout / ".venv" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("", encoding="utf-8")
        (self.checkout / "webapp").mkdir()
        (self.home / "hooks").mkdir(parents=True)
        (self.home / "logs").mkdir()
        self.profile.mkdir()
        for template, file, _script in install.HOOK_CONFIGS:
            text = install.read_text(str(self.copilot / "hooks" / template))
            install.write_text(str(self.home / "hooks" / file),
                               install.render_hook_config(text, str(self.python), str(self.copilot), ""))
        text = install.read_text(str(self.copilot / "hooks" / install.CHIEF_LAUNCHER[0]))
        install.write_text(str(self.home / "hooks" / install.CHIEF_LAUNCHER[1]),
                           install.render_chief_launcher(text, str(self.python), str(self.copilot)))
        installed = self.home / "skills" / "quick"
        shutil.copytree(self.skill, installed)
        (installed / install.SKILL_MARKER).write_text("managed", encoding="utf-8")
        self.state = root / "state"
        (self.state / "chief-inbox").mkdir(parents=True)

    def run(self):
        with mock.patch.object(doctor, "run_git", fake_git), \
                mock.patch.object(doctor.shutil, "which", return_value="copilot"), \
                mock.patch.object(doctor.launcher, "_request", return_value={"chief_doorbell": True}), \
                mock.patch.dict(os.environ, {"COPILOT_HOOKS_STATE_DIR": str(self.state)}):
            doc = doctor.Doctor(self.checkout, self.home, self.profile, runner=fake_runner, issue_ref="lite#1")
            checks, groups = doc.run_all()
        return {f"{c.area}.{c.name}": c for c in checks}, doctor.render(checks, groups, lambda s: s)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.machine = Machine(self.tmp)

    def test_a_healthy_install_passes(self) -> None:
        checks, lines = self.machine.run()
        self.assertEqual(lines[-1], "DOCTOR=pass fails=0 unknowns=0", "\n".join(lines))
        self.assertEqual(checks["verb.issues"].status, doctor.PASS)
        self.assertEqual(checks["mailbox.doorbell"].detail, {"setting": "on", "wake": "on"})

    def test_a_missing_hook_target_fails(self) -> None:
        (self.machine.copilot / "hooks" / "chief_guard.py").unlink()
        checks, lines = self.machine.run()
        self.assertEqual(checks["install.hook:chief-guard.targets"].status, doctor.FAIL)
        self.assertIn("chief_guard.py", checks["install.hook:chief-guard.targets"].detail["missing"])
        self.assertTrue(lines[-1].startswith("DOCTOR=fail "))

    def test_a_stale_installed_skill_fails(self) -> None:
        (self.machine.home / "skills" / "quick" / "SKILL.md").write_text("an older copy\n", encoding="utf-8")
        checks, _lines = self.machine.run()
        self.assertEqual(checks["install.skill:quick"].status, doctor.FAIL)

    def test_a_hook_config_rendered_for_another_checkout_fails(self) -> None:
        target = self.machine.home / "hooks" / install.HOOK_CONFIGS[0][1]
        install.write_text(str(target), install.read_text(str(target)).replace("/lite/", "/lite-old/"))
        checks, _lines = self.machine.run()
        self.assertEqual(checks["install.hook:session-state"].status, doctor.FAIL)

    def test_an_unreadable_mailbox_is_unknown_not_pass(self) -> None:
        inbox = self.machine.state / "chief-inbox"
        inbox.rmdir()
        inbox.write_text("not a folder", encoding="utf-8")
        checks, lines = self.machine.run()
        self.assertEqual(checks["mailbox.events"].status, doctor.UNKNOWN)
        self.assertEqual(lines[-1], "DOCTOR=unknown fails=0 unknowns=1")

    def test_errors_in_the_window_fail_the_logs_check(self) -> None:
        now = datetime.now()
        old = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        (self.machine.checkout / "webapp" / "webapp-warnings.log").write_text(
            f"{old},1 [ERROR] x: too old\n{fresh},1 [ERROR] x: broke 12\n{fresh},2 [ERROR] x: broke 13\n"
            f"{fresh},3 [INFO] x: fine\n", encoding="utf-8")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        (self.machine.home / "logs" / "process-1.log").write_text(
            f"{stamp} [ERROR] [rust:hooks] Invalid hook configuration in a.json\n"
            f"{stamp} [ERROR] SessionClient poll loop error\n", encoding="utf-8")
        checks, lines = self.machine.run()
        self.assertEqual(checks["logs.launcher"].detail["failures"], "2")
        self.assertEqual(checks["logs.copilot"].detail, {"failures": "1", "notes": "1", "window": "24h"})
        self.assertTrue(any('count=2' in ln and 'msg="x: broke N"' in ln for ln in lines), "\n".join(lines))


class RedactionTests(unittest.TestCase):
    def test_paths_user_and_host_are_masked(self) -> None:
        redact = doctor.Redactor([(r"C:\Users\sample", "<home>"), (r"C:\Users\sample\lite", "<checkout>")], "Sample", "PC-7")
        text = r"C:\Users\sample\lite\x C:/users/sample/.copilot C:\\Users\\sample\\y sample@PC-7 resampled"
        self.assertEqual(redact(text), r"<checkout>\x <home>/.copilot <home>\\y <user>@<host> resampled")

    def test_doctor_output_never_names_the_real_home_user_or_host(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        machine = Machine(tmp)
        (machine.copilot / "hooks" / "chief_guard.py").unlink()  # a failure line that prints a full path
        _checks, lines = machine.run()
        redact = doctor.Redactor.for_machine(machine.checkout, machine.home)
        output = "\n".join(redact(line) for line in lines)
        # host-identity-exempt: the privacy check compares the output against the real home, user and host on purpose
        home = os.environ.get("USERPROFILE") or str(Path.home())
        raw = "\n".join(lines).replace("\\", "/").lower()
        self.assertIn(str(tmp).replace("\\", "/").lower(), raw)  # unredacted, the run names the temp folder
        self.assertNotIn(home.lower(), output.lower())
        self.assertNotIn(home.replace("\\", "/").lower(), output.lower())
        # host-identity-exempt: the privacy check compares the output against the real user and host on purpose
        for name in (getpass.getuser(), os.environ.get("COMPUTERNAME") or socket.gethostname()):
            self.assertIsNone(re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", output, re.I))


class LogGroupingTests(unittest.TestCase):
    def test_ids_and_numbers_collapse_into_one_group(self) -> None:
        self.assertEqual(doctor_logs.normalise("09:56:01 [ERROR] lost session 5d70f19bed16 after 12 s"),
                         "lost session <id> after N s")


if __name__ == "__main__":
    unittest.main()
