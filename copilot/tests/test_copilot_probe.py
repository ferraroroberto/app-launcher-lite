"""Hermetic tests for scripts/copilot_probe.py (stdlib unittest, no Copilot needed).

Run:  python -m unittest discover -s tests -v

The live leg itself spends a Copilot request and is run by hand; these cases pin
the parts that decide a status: the report merge (no blank rows, a result from
another CLI version never stays current), the static flag checks, the hook's
answers, and the classification of one live run's hook log.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import copilot_probe as cp  # noqa: E402


def _fake_runner(version: str, help_text: str):
    def run(argv, timeout, **_kw):
        out = version if argv[-1] == "--version" else help_text
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    return run


class MergeTests(unittest.TestCase):
    def test_every_item_gets_a_row_with_status_and_evidence(self) -> None:
        merged = cp.merge({"items": {}}, "1.0.112", {})
        self.assertEqual(set(merged["items"]), set(cp.ITEMS))
        for row in merged["items"].values():
            self.assertEqual(row["status"], "unknown")
            self.assertTrue(row["evidence"])
            self.assertTrue(row["question"])

    def test_same_version_result_carries_forward(self) -> None:
        old = cp.merge({"items": {}}, "1.0.112",
                       {"composer_keys_doorbell": cp._result("verified", "saw it clear and submit")})
        again = cp.merge(old, "1.0.112", {})
        self.assertEqual(again["items"]["composer_keys_doorbell"]["status"], "verified")

    def test_other_version_result_drops_to_unknown_but_keeps_evidence(self) -> None:
        old = cp.merge({"items": {}}, "1.0.105",
                       {"agent_stop_block": cp._result("unsupported", "ended after one stop")})
        new = cp.merge(old, "1.0.112", {})
        row = new["items"]["agent_stop_block"]
        self.assertEqual(row["status"], "unknown")
        self.assertIn("unsupported on CLI 1.0.105", row["evidence"])

    def test_invalid_status_is_coerced_to_unknown(self) -> None:
        merged = cp.merge({"items": {}}, "1.0.112", {"flag_autopilot": {"status": "yes", "evidence": "x"}})
        self.assertEqual(merged["items"]["flag_autopilot"]["status"], "unknown")

    def test_report_round_trips_through_disk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state" / "copilot-probe.json"
            self.assertEqual(cp.load_report(path)["items"], {})
            cp.save_report(path, cp.merge({"items": {}}, "1.0.112", {}))
            self.assertEqual(cp.load_report(path)["cli_version"], "1.0.112")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(cp.load_report(path)["items"], {})


class StaticTests(unittest.TestCase):
    def test_flags_present_absent_and_version(self) -> None:
        help_text = "  --max-ai-credits <n>\n  --session-id <id>\n  --autopilot\n"
        res = cp.static_checks("copilot", _fake_runner("GitHub Copilot CLI 1.0.112.", help_text))
        self.assertEqual(cp.cli_version_of(res), "1.0.112")
        self.assertEqual(res["flag_autopilot"]["status"], "verified")
        self.assertEqual(res["flag_no_ask_user"]["status"], "unsupported")

    def test_missing_cli_is_unknown(self) -> None:
        res = cp.static_checks(None)
        self.assertEqual(res["cli_version"]["status"], "unknown")
        self.assertEqual(cp.cli_version_of(res), "unknown")

    def test_empty_help_is_unknown_not_unsupported(self) -> None:
        res = cp.static_checks("copilot", _fake_runner("1.0.112", ""))
        self.assertEqual(res["flag_session_id"]["status"], "unknown")


class HookTests(unittest.TestCase):
    def test_session_start_injects_and_first_agent_stop_blocks_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "context.txt").write_text("The probe word is W.", encoding="utf-8")
            (folder / "block.txt").write_text("say B", encoding="utf-8")
            start = json.loads(cp.hook_main("sessionStart", folder, '{"sessionId": "s1"}'))
            self.assertEqual(start["additionalContext"], "The probe word is W.")
            first = json.loads(cp.hook_main("agentStop", folder, "{}"))
            self.assertEqual(first["decision"], "block")
            self.assertEqual(cp.hook_main("agentStop", folder, "{}"), "")
            self.assertEqual([e["event"] for e in cp.read_events(folder)],
                             ["sessionStart", "agentStop", "agentStop"])


class ClassifyTests(unittest.TestCase):
    def _events(self, *names: str, sid: str = "") -> list:
        return [{"event": n, "payload": {"sessionId": sid} if sid else {}} for n in names]

    def test_full_success(self) -> None:
        events = self._events("sessionStart", "userPromptSubmitted", "agentStop", "agentStop", "sessionEnd")
        res = cp.classify_live(events, "PW\nBW", "PW", "BW", ran=True)
        self.assertEqual(res["hooks_fire_prompt_mode"]["status"], "verified")
        self.assertEqual(res["session_start_additional_context"]["status"], "verified")
        self.assertEqual(res["agent_stop_block"]["status"], "verified")
        self.assertEqual(res["usage_record_context"]["status"], "unknown")

    def test_hooks_fire_but_nothing_honoured(self) -> None:
        events = self._events("sessionStart", "userPromptSubmitted", "agentStop", "sessionEnd")
        res = cp.classify_live(events, "NONE", "PW", "BW", ran=True)
        self.assertEqual(res["session_start_additional_context"]["status"], "unsupported")
        self.assertEqual(res["agent_stop_block"]["status"], "unsupported")

    def test_no_hooks_and_failed_run_stay_unknown(self) -> None:
        res = cp.classify_live([], "", "PW", "BW", ran=True)
        self.assertEqual(res["session_start_additional_context"]["status"], "unknown")
        self.assertEqual(res["agent_stop_block"]["status"], "unknown")
        failed = cp.classify_live(self._events("sessionStart"), "PW", "PW", "BW", ran=False)
        self.assertTrue(all(r["status"] == "unknown" for r in failed.values()))
        # The deny item comes from its own run (classify_deny), not this one.
        self.assertEqual({k for k, (_q, m) in cp.ITEMS.items() if m == "live"} - {"pre_tool_use_deny"}, set(failed))

    def test_deny_run_classification(self) -> None:
        seen = [{"event": "preToolUse", "payload": {"toolName": "powershell"}}]
        self.assertEqual(cp.classify_deny(seen, tool_ran=False, ran=True)["status"], "verified")
        self.assertEqual(cp.classify_deny(seen, tool_ran=True, ran=True)["status"], "unsupported")
        self.assertEqual(cp.classify_deny([], tool_ran=False, ran=True)["status"], "unknown")
        self.assertEqual(cp.classify_deny([], tool_ran=False, ran=False)["evidence"],
                         "the copilot -p run did not complete")

    def test_usage_record_read_from_events_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            (state / "s1").mkdir()
            line = {"type": "session.usage_record", "data": {"usage": {"inputTokens": 10, "maxPromptTokens": 100}}}
            (state / "s1" / "events.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
            self.assertEqual(cp.usage_record_check("s1", state)["status"], "verified")
            self.assertEqual(cp.usage_record_check("s2", state)["status"], "unknown")
            self.assertEqual(cp.usage_record_check("", state)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
