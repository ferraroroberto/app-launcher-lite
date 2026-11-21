"""Re-runnable Copilot CLI capability probe for the chief port.

The chief's launcher side (app-launcher-lite) leans on Copilot CLI behaviours that
change between releases: whether hooks fire, whether a hook can inject context or
force another turn, what the composer keys do. This script keeps one report of
those facts per installed CLI version, so a `copilot update` or a new machine
re-asks every question instead of trusting an old answer.

Every item ends in exactly one of three states. ``verified`` and ``unsupported``
need evidence; anything not proven on the installed version is ``unknown`` --
a result recorded on another CLI version is carried as evidence only, never as
the current status.

Subcommands
-----------
  run [--live] [--model M]
        Static checks (``copilot --version``, flags in ``--help``) always run and
        spend nothing. ``--live`` adds one ``copilot -p`` request in a throwaway
        trusted folder whose repo-level hooks (``.github/hooks/probe.json``) log
        every event, inject a nonce through ``sessionStart`` and block the first
        ``agentStop``. Prints one ``PROBE`` line per item and a ``PROBE_SUMMARY``.
  record <id> <status> --evidence "<text>"
        Record a manual result (an interactive-only item) for the installed CLI.
  report
        Print the stored report without probing.
  hook <event> --dir <folder>
        Internal: the hook command the live probe installs.

Report: ``~/.copilot/hooks/state/copilot-probe.json`` (``--report`` overrides).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "_lib"))
import runtime_files  # noqa: E402

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
REPORT_VERSION = 1
STATUSES = ("verified", "unsupported", "unknown")
DEFAULT_REPORT = Path.home() / ".copilot" / "hooks" / "state" / "copilot-probe.json"
SESSION_STATE_DIR = Path.home() / ".copilot" / "session-state"
HOOK_EVENTS = ("sessionStart", "userPromptSubmitted", "agentStop", "sessionEnd")
# The second live run also installs preToolUse, which denies every tool call.
DENY_EVENTS = HOOK_EVENTS + ("preToolUse",)
DENY_PROMPT = ("Run this exact shell command, then reply DONE: "
               "python -c \"open('ran.txt','w').write('x')\"")
# One live request: the CLI's own startup plus two model turns (the forced one included).
LIVE_TIMEOUT_SECONDS = 300
STATIC_TIMEOUT_SECONDS = 30

# id -> (question, method). "static": --help/--version, free. "live": the one -p run.
# "manual": needs an interactive TUI driven by a person (or the launcher), recorded by hand.
ITEMS: Dict[str, tuple] = {
    "cli_version": ("installed Copilot CLI version", "static"),
    "flag_max_ai_credits": ("--max-ai-credits flag exists", "static"),
    "flag_session_id": ("--session-id flag exists", "static"),
    "flag_autopilot": ("--autopilot flag exists", "static"),
    "flag_no_ask_user": ("--no-ask-user flag exists", "static"),
    "hooks_fire_prompt_mode": ("a -p run fires sessionStart, userPromptSubmitted, agentStop, sessionEnd", "live"),
    "session_start_additional_context": ("sessionStart additionalContext reaches the model", "live"),
    "agent_stop_block": ("agentStop decision: block forces another turn", "live"),
    "usage_record_context": ("events.jsonl session.usage_record carries inputTokens and maxPromptTokens", "live"),
    "pre_tool_use_deny": ("preToolUse permissionDecision deny stops the tool call", "live"),
    "hooks_fire_interactive": ("an interactive TUI session fires the four hooks", "manual"),
    "prompt_hook_replaces_skill": ("in an interactive session userPromptSubmitted gets a skill invocation already "
                                   "expanded, and its modifiedPrompt replaces it", "manual"),
    "resume_session_id_keeps_context": ("-p --session-id resumes with context kept", "manual"),
    "composer_keys_doorbell": ("composer keys Backspace, Ctrl-U, Enter clear and submit the doorbell line", "manual"),
    "compact_queued_midturn": ("/compact <focus> typed mid-turn runs at turn end", "manual"),
    "remote_on_from_launcher": ("/remote on typed from the launcher enables remote control", "manual"),
    "background_shell_wakes_agent": ("a background shell finishing wakes an idle agent", "manual"),
    "ask_user_blocks_autopilot": ("ask_user blocks for an answer under --autopilot", "manual"),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _copilot() -> Optional[str]:
    return shutil.which("copilot")


def _run(argv: List[str], timeout: int, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 -- argv built here, no shell
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW, **kwargs,
    )


def load_report(path: Path) -> Dict[str, Any]:
    """The stored report, or an empty one when missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": REPORT_VERSION, "cli_version": "", "updated": "", "items": {}}
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return {"version": REPORT_VERSION, "cli_version": "", "updated": "", "items": {}}
    return data


def save_report(path: Path, report: Dict[str, Any]) -> None:
    runtime_files.make_dirs(path.parent)
    tmp = path.with_suffix(".tmp")
    runtime_files.save_text(tmp, json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def merge(report: Dict[str, Any], cli_version: str, results: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """Fold fresh ``results`` into ``report`` for ``cli_version``; every item gets a row.

    A row recorded on another CLI version drops back to ``unknown`` and keeps its
    old status only as evidence, so an update always re-asks the question.
    """
    items: Dict[str, Dict[str, str]] = {}
    old = report.get("items", {})
    for item_id, (question, method) in ITEMS.items():
        if item_id in results:
            row = dict(results[item_id])
        else:
            prev = old.get(item_id) if isinstance(old.get(item_id), dict) else None
            if prev and prev.get("cli_version") == cli_version and prev.get("status") in STATUSES:
                row = {k: str(prev.get(k, "")) for k in ("status", "evidence", "checked")}
            elif prev and prev.get("status") in ("verified", "unsupported"):
                row = {"status": "unknown", "checked": "",
                       "evidence": f"was {prev['status']} on CLI {prev.get('cli_version') or '?'}; re-probe on this version"}
            else:
                row = {"status": "unknown", "checked": "", "evidence": "not probed on this version"}
        if row.get("status") not in STATUSES:
            row["status"] = "unknown"
        if not row.get("evidence"):
            row["evidence"] = "no evidence recorded"
        row.update(question=question, method=method, cli_version=cli_version)
        items[item_id] = row
    return {"version": REPORT_VERSION, "cli_version": cli_version, "updated": _now(), "items": items}


def _result(status: str, evidence: str) -> Dict[str, str]:
    return {"status": status, "evidence": evidence, "checked": _now()}


def static_checks(copilot: Optional[str], runner: Callable[..., Any] = _run) -> Dict[str, Dict[str, str]]:
    """Version and flag presence from ``copilot --version`` / ``--help``; spends nothing."""
    if not copilot:
        return {"cli_version": _result("unknown", "copilot not found on PATH")}
    results: Dict[str, Dict[str, str]] = {}
    try:
        version_out = runner([copilot, "--version"], STATIC_TIMEOUT_SECONDS).stdout
        help_out = runner([copilot, "--help"], STATIC_TIMEOUT_SECONDS).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"cli_version": _result("unknown", f"copilot did not run: {exc}")}
    match = re.search(r"(\d+\.\d+\.\d+)", version_out or "")
    results["cli_version"] = (_result("verified", match.group(1)) if match
                              else _result("unknown", "no version in copilot --version output"))
    for item_id, flag in (("flag_max_ai_credits", "--max-ai-credits"), ("flag_session_id", "--session-id"),
                          ("flag_autopilot", "--autopilot"), ("flag_no_ask_user", "--no-ask-user")):
        if not help_out:
            results[item_id] = _result("unknown", "copilot --help printed nothing")
        elif re.search(rf"(^|\s){re.escape(flag)}\b", help_out):
            results[item_id] = _result("verified", f"{flag} listed in copilot --help")
        else:
            results[item_id] = _result("unsupported", f"{flag} absent from copilot --help")
    return results


def cli_version_of(results: Dict[str, Dict[str, str]]) -> str:
    row = results.get("cli_version", {})
    return row.get("evidence", "") if row.get("status") == "verified" else "unknown"


def write_probe_folder(folder: Path, context_word: str, block_word: str,
                       events: tuple = HOOK_EVENTS, deny: bool = False) -> None:
    """Lay out the throwaway trusted folder: a git repo with logging repo-level hooks."""
    script = Path(__file__).resolve().as_posix()
    python = Path(sys.executable).as_posix()
    hooks = {
        event: [{"type": "command", "timeoutSec": 20,
                 "powershell": f"& '{python}' '{script}' hook {event} --dir '{folder.as_posix()}'",
                 "bash": f"'{python}' '{script}' hook {event} --dir '{folder.as_posix()}'"}]
        for event in events
    }
    runtime_files.make_dirs(folder / ".github" / "hooks")
    runtime_files.save_text(folder / ".github" / "hooks" / "probe.json",
                            json.dumps({"version": 1, "hooks": hooks}, indent=2))
    runtime_files.save_text(folder / "context.txt", f"The probe word is {context_word}.")
    runtime_files.save_text(folder / "block.txt",
                            f"Before stopping, reply with exactly this word on its own line: {block_word}")
    if deny:
        runtime_files.save_text(folder / "deny.txt", "Denied by the capability probe.")
    _run(["git", "init", "-q", str(folder)], STATIC_TIMEOUT_SECONDS)


def hook_main(event: str, folder: Path, stdin_text: str) -> str:
    """The installed hook: log the payload; answer sessionStart and the first agentStop."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {"unparsed": stdin_text[:500]}
    log = folder / "events.log"
    with runtime_files.open_for_write(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "payload": payload}) + "\n")
    if event == "sessionStart" and (folder / "context.txt").exists():
        text = (folder / "context.txt").read_text(encoding="utf-8")
        # Both spellings: Copilot's own and the Claude Code shape it also reads.
        return json.dumps({"additionalContext": text,
                           "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
    if event == "preToolUse" and (folder / "deny.txt").exists():
        reason = (folder / "deny.txt").read_text(encoding="utf-8")
        # Both spellings again: Copilot's own and the Claude Code shape it also reads.
        return json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason,
                           "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                  "permissionDecisionReason": reason}})
    if event == "agentStop" and (folder / "block.txt").exists():
        stops = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if '"agentStop"' in line)
        if stops == 1:
            return json.dumps({"decision": "block", "reason": (folder / "block.txt").read_text(encoding="utf-8")})
    return ""


def read_events(folder: Path) -> List[Dict[str, Any]]:
    try:
        lines = (folder / "events.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def usage_record_check(session_id: str, state_dir: Path = SESSION_STATE_DIR) -> Dict[str, str]:
    if not session_id:
        return _result("unknown", "no sessionId in any hook payload")
    path = state_dir / session_id / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return _result("unknown", f"no events.jsonl for session {session_id}")
    for line in reversed(lines):
        if '"session.usage_record"' not in line:
            continue
        try:
            usage = json.loads(line).get("data", {}).get("usage", {})
        except (ValueError, AttributeError):
            continue
        if isinstance(usage.get("inputTokens"), int) and isinstance(usage.get("maxPromptTokens"), int):
            return _result("verified", f"inputTokens={usage['inputTokens']} maxPromptTokens={usage['maxPromptTokens']}")
        return _result("unsupported", "session.usage_record lacks inputTokens or maxPromptTokens")
    return _result("unknown", "no session.usage_record event in events.jsonl")


def classify_live(events: List[Dict[str, Any]], output: str, context_word: str, block_word: str,
                  ran: bool) -> Dict[str, Dict[str, str]]:
    """Turn one live run's hook log and stdout into results for the live items."""
    fired = [e.get("event") for e in events]
    results: Dict[str, Dict[str, str]] = {}
    missing = [e for e in HOOK_EVENTS if e not in fired]
    if not ran:
        reason = "the copilot -p run did not complete"
        return {k: _result("unknown", reason) for k, (_q, m) in ITEMS.items()
                if m == "live" and k != "pre_tool_use_deny"}
    results["hooks_fire_prompt_mode"] = (_result("verified", "all four events logged") if not missing
                                         else _result("unknown", f"not logged: {', '.join(missing)}"))
    if "sessionStart" not in fired:
        results["session_start_additional_context"] = _result("unknown", "sessionStart did not fire")
    elif context_word in output:
        results["session_start_additional_context"] = _result("verified", "the injected nonce came back in the reply")
    else:
        results["session_start_additional_context"] = _result("unsupported", "sessionStart fired; the nonce never reached the reply")
    stops = fired.count("agentStop")
    if stops == 0:
        results["agent_stop_block"] = _result("unknown", "agentStop did not fire")
    elif stops >= 2 and block_word in output:
        results["agent_stop_block"] = _result("verified", f"agentStop fired {stops}x and the block reason was acted on")
    elif stops == 1:
        results["agent_stop_block"] = _result("unsupported", "the blocking agentStop fired once and the run ended")
    else:
        results["agent_stop_block"] = _result("unknown", f"agentStop fired {stops}x but the block word is absent")
    session_id = next((str(e.get("payload", {}).get("sessionId") or "") for e in events
                       if isinstance(e.get("payload"), dict) and e["payload"].get("sessionId")), "")
    results["usage_record_context"] = usage_record_check(session_id)
    return results


def classify_deny(events: List[Dict[str, Any]], tool_ran: bool, ran: bool) -> Dict[str, str]:
    """The `pre_tool_use_deny` result from the deny run's hook log and the marker file."""
    tools = sorted({str(e.get("payload", {}).get("toolName") or "?") for e in events
                    if e.get("event") == "preToolUse" and isinstance(e.get("payload"), dict)})
    if tool_ran:
        return _result("unsupported", f"the denied command still ran (tools seen: {', '.join(tools) or 'none'})")
    if not tools:
        return _result("unknown", "preToolUse never fired" if ran else "the copilot -p run did not complete")
    return _result("verified", f"denied before running; tools seen: {', '.join(tools)}")


def _live_run(copilot: str, folder: Path, prompt: str, model: str) -> Tuple[str, bool]:
    """One `copilot -p` in `folder`; `(stdout+stderr, exited 0)`."""
    argv = [copilot, "-p", prompt, "--no-ask-user", "--no-auto-update"]
    if model:
        argv += ["--model", model]
    # Exactly "true" trusts the working directory, which is what loads its repo-level hooks.
    env = {**os.environ, "COPILOT_ALLOW_ALL": "true"}
    try:
        proc = _run(argv, LIVE_TIMEOUT_SECONDS, cwd=str(folder), env=env)
        return (proc.stdout or "") + (proc.stderr or ""), proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc), False


def live_checks(copilot: str, model: str = "") -> Dict[str, Dict[str, str]]:
    context_word = f"PROBE{uuid.uuid4().hex[:8].upper()}"
    block_word = f"BLOCK{uuid.uuid4().hex[:8].upper()}"
    folder = Path(tempfile.mkdtemp(prefix="copilot-probe-"))
    write_probe_folder(folder, context_word, block_word)
    output, ran = _live_run(copilot, folder, "Reply with the probe word from your context, or NONE if you have none.",
                            model)
    results = classify_live(read_events(folder), output, context_word, block_word, ran)
    shutil.rmtree(folder, ignore_errors=True)

    folder = Path(tempfile.mkdtemp(prefix="copilot-probe-deny-"))
    write_probe_folder(folder, context_word, block_word, events=DENY_EVENTS, deny=True)
    for name in ("context.txt", "block.txt"):
        (folder / name).unlink()
    _output, ran = _live_run(copilot, folder, DENY_PROMPT, model)
    results["pre_tool_use_deny"] = classify_deny(read_events(folder), (folder / "ran.txt").exists(), ran)
    shutil.rmtree(folder, ignore_errors=True)
    return results


def print_report(report: Dict[str, Any]) -> None:
    counts = {s: 0 for s in STATUSES}
    for item_id, row in report.get("items", {}).items():
        counts[row.get("status", "unknown")] = counts.get(row.get("status", "unknown"), 0) + 1
        print(f"PROBE id={item_id} status={row.get('status')} method={row.get('method')} "
              f"cli={row.get('cli_version')} evidence={row.get('evidence')}")
    print("PROBE_SUMMARY " + " ".join(f"{s}={counts[s]}" for s in STATUSES)
          + f" cli={report.get('cli_version') or 'unknown'}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--live", action="store_true")
    run_p.add_argument("--model", default="")
    rec = sub.add_parser("record")
    rec.add_argument("item", choices=sorted(ITEMS))
    rec.add_argument("status", choices=STATUSES)
    rec.add_argument("--evidence", required=True)
    sub.add_parser("report")
    hook = sub.add_parser("hook")
    hook.add_argument("event")
    hook.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "hook":
        out = hook_main(args.event, args.dir, sys.stdin.read())
        if out:
            print(out)
        return 0
    report = load_report(args.report)
    if args.cmd == "report":
        print_report(merge(report, report.get("cli_version") or "unknown", {}))
        return 0
    copilot = _copilot()
    results = static_checks(copilot)
    cli_version = cli_version_of(results)
    if args.cmd == "record":
        if not args.evidence.strip():
            parser.error("--evidence must say what was observed")
        results[args.item] = _result(args.status, args.evidence.strip())
    elif args.live:
        if copilot:
            results.update(live_checks(copilot, args.model))
        else:
            results.update({k: _result("unknown", "copilot not found on PATH")
                            for k, (_q, m) in ITEMS.items() if m == "live"})
    merged = merge(report, cli_version, results)
    save_report(args.report, merged)
    print_report(merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
