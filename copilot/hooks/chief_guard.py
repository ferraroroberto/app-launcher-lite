"""Chief guard hooks: the handover at session start, holds at turn end, no ask_user.

The lite counterpart of fleet-config's ``chief_handover_sessionstart.py``,
``chief_waiter_stop.py`` and ``block_askuserquestion_chief.py``, in one module
the installer registers for four Copilot events (``chief-guard.template.json``
passes the event as ``argv[1]``). Each behaviour was verified live on Copilot
CLI 1.0.112 by ``scripts/copilot_probe.py``: ``sessionStart`` ``additionalContext``
reaches the model, ``agentStop`` ``decision: block`` forces another turn, and
``preToolUse`` ``permissionDecision: deny`` stops the tool call.

- ``sessionStart``, the chief only: hand back the handover log
  (``<state dir>/chief-handover.md``) as ``additionalContext``, so a fresh
  boot, a resume or a compaction never starts the chief without its intent.
- ``agentStop``, the chief only, never while already continuing after a hold
  (``stop_hook_active``): hold the turn end once per window when the context is
  due for compaction (``chief_ops.py compact-due``) or when worker slots sit idle
  with queued plan rows (``slots-idle``). It never asks for the launcher's
  doorbell: that bell rings only a chief whose composer the session host
  tracks, and it does not track Copilot's, so turning it on would not wake
  this chief.
- ``preToolUse``: deny ``ask_user`` for the chief and for every chief-managed
  lane. It renders only in the session's own terminal, where nobody reads it.
  Also deny a chief-managed lane's shell call that reaches the full fleet (a ``gh``
  command, ``notify_complete``, a helper under the Claude Code user-scope skills
  folder or the full fleet's checkout) and name lite's own skills in the
  reason, because on a PC that also runs the full fleet ``/issue-start`` can resolve
  to that fleet's GitHub skill.
- ``userPromptSubmitted``, a session app-launcher-lite's session-host lists: when the
  prompt carries a skill this checkout also ships, expanded from somewhere else (the
  full fleet's user-scope skills folder on a shared PC), return ``modifiedPrompt`` with
  that skill block holding this checkout's ``SKILL.md`` instead. The swap happens in the
  prompt itself, before the first model turn, so no model has to choose between the
  skill it was handed and a later redirect. On a dedicated machine the skill already
  comes from this checkout and the prompt is left alone.

Who is the chief: a launcher session (``APP_LAUNCHER_SESSION_ID``) whose working
directory is a fleet-config-lite checkout and whose id ``chief_ops.py chief-sid``
names. Every other session is allowed before any subprocess runs. Fail-open: a
fact that cannot be read allows the action, and that is logged to stderr, never
treated as fine.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402
import chief_inbox  # noqa: E402
import session_state  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CHIEF_OPS = REPO / "skills" / "_lib" / "chief_ops.py"
CHIEF_DIR_PREFIX = "fleet-config-lite"
ASK_TOOL = "ask_user"
SHELL_TOOLS = ("powershell", "bash", "shell")
# What only the full fleet reaches: the skills folder of its Claude Code user scope and of its own
# checkout (`fleet-config/`, never this repo's `fleet-config-lite/`), and its completion ping. Its
# hooks folder is not listed: the full fleet's own preToolUse hook runs from there on every command.
FULL_FLEET_PATH_RE = re.compile(r"[\\/]\.claude[\\/]skills[\\/]|[\\/]fleet-config[\\/]skills[\\/]", re.IGNORECASE)
# That same hook rewrites a shell command to `... context_filter_cli.py run ... --encoded <base64>`
# before this guard runs, so the command the lane typed is the encoded part.
ENCODED_COMMAND_RE = re.compile(r"--encoded\s+([A-Za-z0-9+/=]+)")
NOTIFY_RE = re.compile(r"\bnotify_complete\b", re.IGNORECASE)
# Only a statement that starts with `gh` is a gh command: a commit message or a path that
# merely contains the letters is not.
STATEMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\r\n]")
GH_STATEMENT_RE = re.compile(r"""^\s*(?:&\s*)?['"]?(?:[^\s'"]*[\\/])?gh(?:\.exe)?['"]?(?:\s|$)""", re.IGNORECASE)
# Copilot CLI 1.0.114 hands userPromptSubmitted a skill invocation already expanded:
# `<skill-context name="X">`, `Base directory for this skill: <dir>`, the SKILL.md body
# without its frontmatter, then `ARGUMENTS: <args>` and `</skill-context>`.
SKILL_BLOCK_RE = re.compile(
    r'<skill-context name="(?P<name>[\w-]+)">\s*Base directory for this skill: (?P<base>[^\r\n]+)'
    r'.*?(?:\nARGUMENTS: (?P<args>[^\r\n]*)\r?\n)?</skill-context>', re.DOTALL)
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
HANDOVER_FILENAME = "chief-handover.md"
# The handover is prose the chief wrote for itself; past this it is truncated, newest kept.
HANDOVER_MAX_CHARS = 24000
# Each hold fires at most once per window, so a hold can never loop the chief.
HOLD_WINDOW_S = 600
CHIEF_OPS_TIMEOUT_S = 20
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _log(message: str) -> None:
    print(f"chief_guard: {message}", file=sys.stderr)


def chief_ops(*args: str) -> Optional[str]:
    """`chief_ops.py <args>` stdout, or None when it could not run."""
    try:
        res = subprocess.run(  # noqa: S603 -- this repo's own helper, argv built here
            [sys.executable, str(CHIEF_OPS), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=CHIEF_OPS_TIMEOUT_S, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"chief_ops {args[0]} did not run ({type(exc).__name__})")
        return None
    return res.stdout


def is_chief(payload: Dict[str, Any], sid: str) -> bool:
    """True only when the launcher names this session as the live chief."""
    if not sid or not _lib.cwd(payload).name.startswith(CHIEF_DIR_PREFIX):
        return False
    out = chief_ops("chief-sid")
    if out is None:
        return False
    for line in out.splitlines():
        if line.startswith("CHIEF_SID="):
            return line.split("=", 1)[1].strip() == sid
    _log("chief-sid printed no CHIEF_SID line; treating this session as not the chief")
    return False


def _line(out: Optional[str], prefix: str) -> Optional[str]:
    for line in (out or "").splitlines():
        if line.startswith(prefix):
            return line
    return None


def _recent(name: str, now: float) -> bool:
    try:
        return now - (chief_inbox.state_dir() / name).stat().st_mtime < HOLD_WINDOW_S
    except OSError:
        return False


def _stamp(name: str) -> None:
    try:
        # runtime-write-exempt: a marker under the user state dir (state_dir()), never this repo
        (chief_inbox.state_dir() / name).write_text(str(int(time.time())), encoding="utf-8")
    except OSError as exc:
        _log(f"could not write {name} ({exc}); the next turn end may hold again")


def compact_hold(now: float) -> Optional[str]:
    line = _line(chief_ops("compact-due"), "COMPACT_DUE")
    if not line or not line.startswith("COMPACT_DUE percent="):
        if line and line.startswith("COMPACT_DUE=unknown"):
            _log(f"compact state unknown ({line}); not holding")
        return None
    if _recent("chief-compact-nudge", now) or _recent("chief-compact-queued", now):
        return None
    _stamp("chief-compact-nudge")
    return (f"{line}. Refresh the handover log and the plan, run `chief_ops.py self-compact`, "
            "and end the turn once it prints COMPACT_QUEUED.")


def slots_hold(now: float) -> Optional[str]:
    out = chief_ops("slots-idle")
    line = _line(out, "SLOTS_IDLE=")
    value = line[len("SLOTS_IDLE="):].split()[0] if line else ""
    if not line or _line(out, "SLOTS_COVERED=") or value in ("0", "off"):
        return None
    if _recent("chief-slots-nudge", now):
        return None
    _stamp("chief-slots-nudge")
    return (f"{line}: worker slots are free while plan rows wait. Dispatch from the top of the plan "
            "queue, or ask Roberto with `chief_ops.py plan ask ... --id slots-idle`, then end the turn.")


def on_agent_stop(payload: Dict[str, Any], sid: str) -> Optional[Dict[str, Any]]:
    if payload.get("stop_hook_active") or not is_chief(payload, sid):
        return None
    now = time.time()
    for hold in (compact_hold, slots_hold):
        reason = hold(now)
        if reason:
            return {"decision": "block", "reason": reason}
    return None


def on_session_start(payload: Dict[str, Any], sid: str) -> Optional[Dict[str, Any]]:
    if not is_chief(payload, sid):
        return None
    path = chief_inbox.state_dir() / HANDOVER_FILENAME
    try:
        log = path.read_text(encoding="utf-8")
        text = ("Your handover log (" + HANDOVER_FILENAME + "), as you last wrote it. Live Board and GitLab "
                "state win on facts; this carries intent.\n\n" + log[-HANDOVER_MAX_CHARS:])
    except FileNotFoundError:
        text = f"No handover log yet: start {path.as_posix()} at the first checkpoint."
    except OSError as exc:
        text = f"The handover log could not be read ({type(exc).__name__}); live data is all you have."
    # Both spellings: Copilot's own and the Claude Code shape it also reads.
    return {"additionalContext": text,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def _unwrapped(command: str) -> str:
    """The command a lane typed, when another hook has rewritten it to a wrapper with `--encoded`."""
    match = ENCODED_COMMAND_RE.search(command)
    if not match:
        return command
    try:
        return base64.b64decode(match.group(1), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return command


def foreign_command(payload: Dict[str, Any]) -> Optional[str]:
    """What a shell tool call reaches that belongs to the full fleet, else None.

    Both fields of the payload are searched as text because Copilot sends ``toolArgs`` as a JSON
    string and a future harness may send an object; the shell tools are the only ones checked.
    """
    if payload.get("tool_name") not in SHELL_TOOLS:
        return None
    args = payload.get("tool_args")
    text = args if isinstance(args, str) else json.dumps(args or "")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
            text = parsed["command"]
    except ValueError:
        pass
    text = _unwrapped(text)
    if FULL_FLEET_PATH_RE.search(text):
        return "a helper from the full fleet's skills folder"
    if NOTIFY_RE.search(text):
        return "the full fleet's completion notification"
    for statement in STATEMENT_SPLIT_RE.split(text):
        if GH_STATEMENT_RE.match(statement):
            return "a `gh` command"
    return None


def lane_redirect(foreign: str) -> Dict[str, Any]:
    """The deny answer for a lane that reached for the full fleet, naming the skills it must use."""
    skills = (REPO / "skills").as_posix()
    why = (f"Refused: {foreign}. This is a fleet-config-lite lane on GitLab, and on a PC that also runs the "
           f"full fleet the skill text you were handed may be that fleet's GitHub one. Follow "
           f"{skills}/issue-start/SKILL.md (or issue-yolo / issue-finish there) instead, use `glab` and the "
           f"helpers under {skills}/_lib, and send no completion notification.")
    return {"permissionDecision": "deny", "permissionDecisionReason": why,
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def on_pre_tool_use(payload: Dict[str, Any], sid: str) -> Optional[Dict[str, Any]]:
    if not sid:
        return None
    tool = payload.get("tool_name")
    foreign = foreign_command(payload) if tool != ASK_TOOL else None
    if tool != ASK_TOOL and foreign is None:
        return None
    _row, reason = chief_inbox.managed_row(sid)
    if reason == chief_inbox.UNDETERMINED:
        _log(f"chief-managed registry unreadable; {tool} allowed but unverified")
    if foreign is not None:
        return lane_redirect(foreign) if reason == chief_inbox.MANAGED else None
    if reason != chief_inbox.MANAGED and not is_chief(payload, sid):
        return None
    why = ("ask_user is disabled in a chief session or a chief-managed lane: it renders only in this "
           "terminal, where nobody reads it. State the question and its options as plain output text, "
           "then end the turn or continue with your best judgement.")
    return {"permissionDecision": "deny", "permissionDecisionReason": why,
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def _same_dir(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def lite_skill_swap(prompt: str) -> Optional[str]:
    """The prompt with a foreign copy of one of this checkout's skills replaced by lite's, else None."""
    match = SKILL_BLOCK_RE.search(prompt)
    if not match:
        return None
    name = match.group("name")
    skill_dir = REPO / "skills" / name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file() or _same_dir(Path(match.group("base").strip()), skill_dir):
        return None
    try:
        body = FRONTMATTER_RE.sub("", skill_md.read_text(encoding="utf-8"), count=1).strip()
    except OSError as exc:
        _log(f"lite's {name} skill unreadable ({type(exc).__name__}); prompt left as handed")
        return None
    block = f'<skill-context name="{name}">\nBase directory for this skill: {skill_dir}\n\n{body}\n\n\n'
    if match.group("args") is not None:
        block += f"ARGUMENTS: {match.group('args')}\n"
    block += "</skill-context>"
    _log(f"/{name} expanded from {match.group('base').strip()}; swapped for {skill_dir}")
    return prompt[:match.start()] + block + prompt[match.end():]


def on_user_prompt(payload: Dict[str, Any], sid: str) -> Optional[Dict[str, Any]]:
    if not sid:
        return None
    swapped = lite_skill_swap(str(payload.get("prompt") or ""))
    if swapped is None or not session_state.lite_session(sid):
        return None
    return {"modifiedPrompt": swapped}


HANDLERS = {"sessionStart": on_session_start, "agentStop": on_agent_stop, "preToolUse": on_pre_tool_use,
            "userPromptSubmitted": on_user_prompt}


def main() -> None:
    try:
        handler = HANDLERS.get(sys.argv[1] if len(sys.argv) > 1 else "")
        if handler is not None:
            result = handler(_lib.read_stdin_json(), os.environ.get(chief_inbox.LAUNCHER_SESSION_ENV, "").strip())
            if result:
                print(json.dumps(result))
    except Exception as exc:  # noqa: BLE001 -- a guard must never break the session it guards
        _log(f"failed open ({type(exc).__name__}: {exc})")
    sys.exit(0)


if __name__ == "__main__":
    main()
