"""Tests for hooks/chief_guard.py: the chief's handover, turn-end holds and ask_user deny.

Run:  python -m unittest discover -s tests -v

`chief_ops.py` is replaced by a fake keyed on its verb, so each hold is driven
by the exact line the real verb prints; one case runs the real entry point as
the template's command would.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))

import chief_guard  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHIEF_CWD = "E:/automation/fleet-config-lite"


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name)
        env = mock.patch.dict(os.environ, {"COPILOT_HOOKS_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.lines = {"chief-sid": "CHIEF_SID=chief-1\n", "compact-due": "COMPACT_NOT_DUE percent=10 threshold=30\n",
                      "slots-idle": "SLOTS_IDLE=0 free=3 queued=0\n", "wake": "WAKE=waiter doorbell=off\n"}
        self.calls: list = []
        fake = mock.patch.object(chief_guard, "chief_ops", side_effect=self._fake_ops)
        fake.start()
        self.addCleanup(fake.stop)

    def _fake_ops(self, *args: str):
        self.calls.append(args[0])
        return self.lines.get(args[0])

    def chief_payload(self, **extra) -> dict:
        return {"session_id": "cop-1", "cwd": CHIEF_CWD, **extra}

    def test_only_a_fleet_config_lite_session_pays_for_the_chief_read(self) -> None:
        self.assertFalse(chief_guard.is_chief({"cwd": "E:/automation/other-repo"}, "chief-1"))
        self.assertFalse(chief_guard.is_chief({"cwd": CHIEF_CWD}, ""))
        self.assertEqual(self.calls, [])
        self.assertTrue(chief_guard.is_chief({"cwd": CHIEF_CWD + "-wt-3"}, "chief-1"))
        self.assertFalse(chief_guard.is_chief({"cwd": CHIEF_CWD}, "lane-9"))
        self.lines["chief-sid"] = None
        self.assertFalse(chief_guard.is_chief({"cwd": CHIEF_CWD}, "chief-1"))

    def test_session_start_hands_back_the_handover_log(self) -> None:
        missing = chief_guard.on_session_start(self.chief_payload(), "chief-1")
        self.assertIn("No handover log yet", missing["additionalContext"])
        (self.state / "chief-handover.md").write_text("Batch: lite port, step 7.", encoding="utf-8")
        out = chief_guard.on_session_start(self.chief_payload(), "chief-1")
        self.assertTrue(out["additionalContext"].endswith("Batch: lite port, step 7."))
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], out["additionalContext"])
        self.assertIsNone(chief_guard.on_session_start(self.chief_payload(), "lane-9"))

    def test_agent_stop_holds_once_for_a_due_compaction(self) -> None:
        self.lines["compact-due"] = "COMPACT_DUE percent=82 threshold=30\n"
        first = chief_guard.on_agent_stop(self.chief_payload(), "chief-1")
        self.assertEqual(first["decision"], "block")
        self.assertIn("self-compact", first["reason"])
        self.assertIsNone(chief_guard.on_agent_stop(self.chief_payload(), "chief-1"))
        self.assertIsNone(chief_guard.on_agent_stop(self.chief_payload(stop_hook_active=True), "chief-1"))

    def test_unknown_compact_state_never_holds(self) -> None:
        self.lines["compact-due"] = "COMPACT_DUE=unknown reason=no session.usage_record\n"
        self.assertIsNone(chief_guard.on_agent_stop(self.chief_payload(), "chief-1"))

    def test_idle_slots_hold_unless_covered(self) -> None:
        self.lines["slots-idle"] = "SLOTS_IDLE=2 free=2 queued=4\nSLOTS_COVERED=slots-idle\n"
        self.assertIsNone(chief_guard.on_agent_stop(self.chief_payload(), "chief-1"))
        self.lines["slots-idle"] = "SLOTS_IDLE=2 free=2 queued=4\n"
        out = chief_guard.on_agent_stop(self.chief_payload(), "chief-1")
        self.assertIn("SLOTS_IDLE=2", out["reason"])

    def test_working_lanes_never_make_the_chief_ask_for_the_doorbell(self) -> None:
        (self.state / "chief-managed.json").write_text(json.dumps({"lane-1": {"repo": "demo", "number": 1}}),
                                                       encoding="utf-8")
        (self.state / "sessions-state.json").write_text(json.dumps(
            {"c": {"status": "working", "launcher_session_id": "lane-1"}}), encoding="utf-8")
        self.lines["wake"] = "WAKE=waiter doorbell=off\n"
        self.assertIsNone(chief_guard.on_agent_stop(self.chief_payload(), "chief-1"))

    def test_ask_user_denied_for_the_chief_and_managed_lanes_only(self) -> None:
        ask = {"tool_name": "ask_user", "cwd": "E:/automation/demo"}
        self.assertIsNone(chief_guard.on_pre_tool_use({"tool_name": "powershell", "cwd": CHIEF_CWD}, "chief-1"))
        self.assertIsNone(chief_guard.on_pre_tool_use(ask, "lane-1"))
        (self.state / "chief-managed.json").write_text(json.dumps({"lane-1": {}}), encoding="utf-8")
        self.assertEqual(chief_guard.on_pre_tool_use(ask, "lane-1")["permissionDecision"], "deny")
        chief = chief_guard.on_pre_tool_use({**ask, "cwd": CHIEF_CWD}, "chief-1")
        self.assertEqual(chief["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(chief_guard.on_pre_tool_use(ask, ""))


    def _shell(self, command: str, tool: str = "powershell") -> dict:
        return {"tool_name": tool, "tool_args": json.dumps({"command": command}), "cwd": "E:/automation/demo"}

    def test_a_managed_lane_cannot_reach_the_full_fleet(self) -> None:
        (self.state / "chief-managed.json").write_text(json.dumps({"lane-1": {}}), encoding="utf-8")
        skills = (chief_guard.REPO / "skills").as_posix()
        for command in ("gh issue view 5", "& gh.exe pr list", "git status; gh api repos/x/y",
                        "python C:/Users/u/.claude/skills/_lib/worktree_claim.py acquire .",
                        "python E:/dev/fleet-config/skills/_lib/active_issue.py add . 5 b",
                        r"python C:\Users\u\.claude\hooks\notify_complete.py --kind start", "python notify_complete.py"):
            denied = chief_guard.on_pre_tool_use(self._shell(command), "lane-1")
            self.assertEqual(denied["permissionDecision"], "deny", command)
            self.assertIn(f"{skills}/issue-start/SKILL.md", denied["permissionDecisionReason"])
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(chief_guard.on_pre_tool_use(self._shell("gh issue view 5", "bash"), "lane-1")["permissionDecision"], "deny")

    def test_lite_commands_and_other_sessions_are_left_alone(self) -> None:
        (self.state / "chief-managed.json").write_text(json.dumps({"lane-1": {}}), encoding="utf-8")
        for command in ("glab issue view 5", "git commit -m 'use gh later'", "ls E:/automation/fleet-config-lite/skills",
                        "python E:/automation/fleet-config-lite/skills/_lib/worktree_claim.py acquire .",
                        "echo ghost; ghost.exe run", "git checkout -b fix/gh-5"):
            self.assertIsNone(chief_guard.on_pre_tool_use(self._shell(command), "lane-1"), command)
        # A view tool reading a path is not a command, and a session that no chief dispatched is not a lane.
        self.assertIsNone(chief_guard.on_pre_tool_use({"tool_name": "view", "tool_args": "C:/u/.claude/skills/x"}, "lane-1"))
        self.assertIsNone(chief_guard.on_pre_tool_use(self._shell("gh issue view 5"), "other-sid"))
        self.assertIsNone(chief_guard.on_pre_tool_use(self._shell("gh issue view 5"), ""))

    def test_a_command_another_hook_wrapped_is_judged_by_what_the_lane_typed(self) -> None:
        # The full fleet's own preToolUse hook rewrites `<cmd>` to this wrapper, whose path sits in
        # that fleet's hooks folder; its presence alone must not read as a lane using the full fleet.
        (self.state / "chief-managed.json").write_text(json.dumps({"lane-1": {}}), encoding="utf-8")

        def wrapped(command: str) -> str:
            encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
            return ("& 'C:/py/python.exe' 'E:/automation/fleet-config/hooks/context_filter_cli.py' run "
                    f"--tool PowerShell --mode rewrite --encoded {encoded} --cwd 'E:/x' --agent copilot")

        self.assertIsNone(chief_guard.on_pre_tool_use(self._shell(wrapped("git status --short")), "lane-1"))
        self.assertIsNone(chief_guard.on_pre_tool_use(self._shell(wrapped("glab issue view 5")), "lane-1"))
        for command in ("gh issue view 5", "python C:/u/.claude/skills/_lib/worktree_claim.py acquire ."):
            denied = chief_guard.on_pre_tool_use(self._shell(wrapped(command)), "lane-1")
            self.assertEqual(denied["permissionDecision"], "deny", command)
        # An encoding that is not base64 text is judged as typed, never raising.
        self.assertIsNone(chief_guard.on_pre_tool_use(self._shell("run --encoded !!!"), "lane-1"))

    def test_an_unreadable_registry_does_not_deny_a_foreign_command(self) -> None:
        (self.state / "chief-managed.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(chief_guard.on_pre_tool_use(self._shell("gh issue view 5"), "lane-1"))


def foreign_skill_prompt(name: str = "issue-start", args: str = "135 --brief C:/briefs/b.md") -> str:
    """A prompt shaped as Copilot CLI 1.0.114 hands userPromptSubmitted a skill another tree expanded."""
    base = "C:/Users/u/.copilot/skills/" + name
    return (f'The user explicitly invoked the "/{name}" skill. Follow its instructions now.\n\n'
            f'<skill-context name="{name}">\nBase directory for this skill: {base}\n\n'
            f"Additional skill files available if needed (use as directed by SKILL.md):\n  - {base}/evals/x.md\n\n"
            f"# {name}\n\nRun gh issue view and notify_complete.\n\n\nARGUMENTS: {args}\n</skill-context>")


class SkillSwapTests(unittest.TestCase):
    def setUp(self) -> None:
        hosted = mock.patch.object(chief_guard.session_state, "lite_session", return_value=True)
        self.lite_session = hosted.start()
        self.addCleanup(hosted.stop)

    def test_a_foreign_issue_skill_is_swapped_for_lites_in_a_lite_session(self) -> None:
        out = chief_guard.on_user_prompt({"prompt": foreign_skill_prompt()}, "lane-1")
        prompt = out["modifiedPrompt"]
        skill_dir = chief_guard.REPO / "skills" / "issue-start"
        self.assertTrue(prompt.startswith('The user explicitly invoked the "/issue-start" skill.'))
        self.assertIn(f"Base directory for this skill: {skill_dir}\n", prompt)
        self.assertIn("# issue-start (lite, GitLab)", prompt)
        self.assertNotIn("name: issue-start", prompt)
        self.assertNotIn(".copilot/skills", prompt)
        self.assertNotIn("notify_complete", prompt)
        self.assertTrue(prompt.endswith("\n\n\nARGUMENTS: 135 --brief C:/briefs/b.md\n</skill-context>"))
        self.lite_session.assert_called_once_with("lane-1")

    def test_a_skill_invoked_without_arguments_keeps_that_shape(self) -> None:
        prompt = foreign_skill_prompt("issue-finish").replace("\nARGUMENTS: 135 --brief C:/briefs/b.md", "")
        swapped = chief_guard.on_user_prompt({"prompt": prompt}, "lane-1")["modifiedPrompt"]
        self.assertNotIn("ARGUMENTS:", swapped)
        self.assertIn(str(chief_guard.REPO / "skills" / "issue-finish"), swapped)

    def test_prompts_lite_already_owns_or_does_not_ship_are_left_alone(self) -> None:
        own = foreign_skill_prompt().replace("C:/Users/u/.copilot/skills/issue-start",
                                             str(chief_guard.REPO / "skills" / "issue-start"))
        for prompt in (own, foreign_skill_prompt("design-review"), "/issue-start 5", "fix the button", ""):
            self.assertIsNone(chief_guard.on_user_prompt({"prompt": prompt}, "lane-1"), prompt[:60])
        self.lite_session.assert_not_called()

    def test_a_session_lites_host_does_not_list_keeps_its_skill(self) -> None:
        self.lite_session.return_value = False
        self.assertIsNone(chief_guard.on_user_prompt({"prompt": foreign_skill_prompt()}, "full-fleet-1"))
        self.assertIsNone(chief_guard.on_user_prompt({"prompt": foreign_skill_prompt()}, ""))


class EntryPointTests(unittest.TestCase):
    def test_pre_tool_use_entry_point_denies_ask_user_for_a_managed_lane(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "chief-managed.json").write_text(json.dumps({"lane-1": {}}), encoding="utf-8")
            env = {**os.environ, "COPILOT_HOOKS_STATE_DIR": td, "APP_LAUNCHER_SESSION_ID": "lane-1", "PYTHONUTF8": "1"}
            proc = subprocess.run([sys.executable, str(REPO / "hooks" / "chief_guard.py"), "preToolUse"],
                                  input=json.dumps({"toolName": "ask_user", "cwd": td}), capture_output=True,
                                  text=True, timeout=30, env=env, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["permissionDecision"], "deny")

    def test_pre_tool_use_entry_point_denies_gh_for_a_managed_lane(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "chief-managed.json").write_text(json.dumps({"lane-1": {}}), encoding="utf-8")
            env = {**os.environ, "COPILOT_HOOKS_STATE_DIR": td, "APP_LAUNCHER_SESSION_ID": "lane-1", "PYTHONUTF8": "1"}
            payload = {"toolName": "powershell", "toolArgs": json.dumps({"command": "gh issue view 5"}), "cwd": td}
            proc = subprocess.run([sys.executable, str(REPO / "hooks" / "chief_guard.py"), "preToolUse"],
                                  input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
                                  creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["permissionDecision"], "deny")

    def test_user_prompt_entry_point_swaps_the_skill_for_a_session_lites_host_lists(self) -> None:
        class Host(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 -- http.server's handler name
                body = json.dumps({"sessions": [{"session_id": "lane-1"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Host)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/api/sessions"
        for sid, swapped in (("lane-1", True), ("other-1", False)):
            env = {**os.environ, "FLEET_LITE_SESSION_HOST": url, "APP_LAUNCHER_SESSION_ID": sid, "PYTHONUTF8": "1"}
            proc = subprocess.run([sys.executable, str(REPO / "hooks" / "chief_guard.py"), "userPromptSubmitted"],
                                  input=json.dumps({"sessionId": "cop-1", "prompt": foreign_skill_prompt()}),
                                  capture_output=True, text=True, timeout=30, env=env, creationflags=NO_WINDOW)
            self.assertEqual(proc.returncode, 0)
            if swapped:
                self.assertIn("# issue-start (lite, GitLab)", json.loads(proc.stdout)["modifiedPrompt"])
            else:
                self.assertEqual(proc.stdout.strip(), "")

    def test_template_registers_the_four_events_on_this_module(self) -> None:
        hooks = json.loads((REPO / "hooks" / "chief-guard.template.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hooks), {"sessionStart", "userPromptSubmitted", "agentStop", "preToolUse"})
        for event, entries in hooks.items():
            self.assertTrue(entries[0]["powershell"].endswith(f"/hooks/chief_guard.py' {event}"))


if __name__ == "__main__":
    unittest.main()
