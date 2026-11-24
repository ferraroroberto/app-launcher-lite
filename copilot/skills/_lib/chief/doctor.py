"""`chief_ops.py doctor`: a read-only check of this machine's chief setup.

It runs the same way on every computer and prints one ``CHECK <area>.<name>=<status> ...``
line per fact, then ``DOCTOR=pass|fail|unknown fails=<n> unknowns=<n>``:

* install: the checkout's commit and how far it is behind ``origin/main`` (as of the last
  fetch: doctor never fetches), the Copilot CLI version, the ``.venv`` interpreter, each
  installed hook config against what ``copilot/install.py`` would render now, every hook
  command's target file, the installed skills, and the chief launcher (present, current,
  and answering ``chief-sid``);
* verbs: each read verb the chief uses runs as a child, with its expected line and its time;
* mailbox: pending and claimed-but-unacked events with the oldest age of each, and whether
  the launcher's doorbell switch agrees with ``wake``;
* logs: errors, tracebacks and hook failures of the last 24 hours (``doctor_logs.py``).

A check that cannot establish its fact is ``unknown``, never ``pass``. Doctor writes
nothing: no git mutation, no restart, no delete, no message. Every printed line passes
:class:`Redactor`, so the real home folder, user name, host name and checkout path never
reach the output.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from fleet_core import hooks_state
from git_run import run_git
from no_window import NO_WINDOW

from . import doctor_logs, launcher, mailbox

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
CHIEF_OPS = Path(__file__).resolve().parents[1] / "chief_ops.py"
# The checkout this code sits in: copilot/skills/_lib/chief/doctor.py.
DEFAULT_CHECKOUT = Path(__file__).resolve().parents[4]
# A read verb that needs longer than this is itself the finding (the chief's own calls time out sooner).
VERB_TIMEOUT_S = 90
CLI_TIMEOUT_S = 30
# A pending event older than the doorbell's fallback waiter was not answered by either wake path.
PENDING_STALE_S = mailbox.FALLBACK_WAITER_SECONDS
LOG_GROUPS_SHOWN = 10
LINE_CHARS = 140

Runner = Callable[[Sequence[str], float], Tuple[int, str]]


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: Dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        parts = [f"CHECK {self.area}.{self.name}={self.status}"]
        for key, value in self.detail.items():
            text = str(value).replace('"', "'")
            parts.append(f'{key}="{text}"' if (" " in text or not text) else f"{key}={text}")
        return " ".join(parts)


class Redactor:
    """Masks paths, the user name and the host name in every printed line."""

    def __init__(self, paths: Sequence[Tuple[str, str]], user: str, host: str) -> None:
        self.rules: List[Tuple[re.Pattern, str]] = []
        # Longest first, so the checkout under the home folder reads <checkout>, not <home>\...
        for value, mask in sorted(((v, m) for v, m in paths if v), key=lambda vm: -len(vm[0])):
            value = value.rstrip("\\/")
            variants = {value, value.replace("\\", "/"), value.replace("\\", "\\\\"), value.replace("/", "\\")}
            for variant in sorted(variants, key=len, reverse=True):
                self.rules.append((re.compile(re.escape(variant), re.IGNORECASE), mask))
        for value, mask in ((user, "<user>"), (host, "<host>")):
            if value:
                self.rules.append((re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])",
                                              re.IGNORECASE), mask))

    def __call__(self, text: str) -> str:
        for pattern, mask in self.rules:
            text = pattern.sub(mask, text)
        return text

    @classmethod
    def for_machine(cls, checkout: Path, copilot_home: Path) -> "Redactor":
        home = os.environ.get("USERPROFILE") or str(Path.home())
        try:
            user = getpass.getuser()
        except (OSError, KeyError, ImportError):
            user = os.environ.get("USERNAME", "")
        host = os.environ.get("COMPUTERNAME") or socket.gethostname()
        return cls([(str(checkout), "<checkout>"), (str(copilot_home), "<copilot-home>"), (home, "<home>")],
                   user, host)


def run_child(argv: Sequence[str], timeout: float) -> Tuple[int, str]:
    """Exit code and stdout of one read-only child; raises on timeout or a spawn failure."""
    proc = subprocess.run(  # noqa: S603 -- argv built here from this checkout's own paths
        list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    return proc.returncode, proc.stdout


def _short(text: str) -> str:
    return text.strip().splitlines()[0][:LINE_CHARS] if text.strip() else ""


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


class Doctor:
    """One read-only run over a checkout and a Copilot home."""

    def __init__(self, checkout: Path, copilot_home: Path, profile: Path, runner: Runner = run_child,
                 base_url: str = launcher.DEFAULT_BASE_URL, issue_ref: Optional[str] = None) -> None:
        self.checkout = checkout
        self.copilot_dir = checkout / "copilot"
        self.home = copilot_home
        self.profile = profile
        self.run = runner
        self.base_url = base_url
        repo_name = re.sub(r"-wt-\d+$", "", checkout.name)
        self.issue_ref = issue_ref or f"{repo_name}#1"
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        self.install = importlib.import_module("install")
        self.checks: List[Check] = []
        self.verb_lines: Dict[str, str] = {}

    # --- install ---------------------------------------------------------------------------

    def check_commit(self) -> Check:
        head = run_git(["-C", str(self.checkout), "rev-parse", "--short", "HEAD"], timeout=CLI_TIMEOUT_S)
        if head.returncode != 0:
            return Check("install", "commit", UNKNOWN, {"reason": _short(head.stderr) or "git rev-parse failed"})
        behind = run_git(["-C", str(self.checkout), "rev-list", "--count", "HEAD..origin/main"], timeout=CLI_TIMEOUT_S)
        detail = {"sha": head.stdout.strip()}
        if behind.returncode != 0 or not behind.stdout.strip().isdigit():
            detail["behind"] = "unknown"
            return Check("install", "commit", UNKNOWN, detail)
        detail.update(behind=behind.stdout.strip(), as_of="last-fetch")
        return Check("install", "commit", PASS if detail["behind"] == "0" else FAIL, detail)

    def check_copilot_cli(self) -> Check:
        found = shutil.which("copilot")
        if not found:
            return Check("install", "copilot_cli", FAIL, {"reason": "copilot not on PATH"})
        code, out = self.run([found, "--version"], CLI_TIMEOUT_S)
        version = _short(out)
        if code != 0 or not version:
            return Check("install", "copilot_cli", UNKNOWN, {"reason": f"--version exit {code}"})
        return Check("install", "copilot_cli", PASS, {"version": version})

    def check_venv(self) -> Check:
        for candidate in (self.checkout / ".venv" / "Scripts" / "python.exe", self.checkout / ".venv" / "bin" / "python"):
            if candidate.is_file():
                return Check("install", "venv", PASS, {"python": str(candidate)})
        return Check("install", "venv", FAIL, {"reason": "no .venv interpreter in the checkout"})

    def shared_machine(self) -> bool:
        """The full fleet runs here too: install then writes no skill and scopes the session hook."""
        for folder in (self.home / "skills", self.profile / ".agents" / "skills"):
            target = self.install.link_target(str(folder))
            if target and not self.install.starts_under(target, str(self.checkout)):
                return True
        return False

    def check_hooks(self, shared: bool) -> List[Check]:
        checks = []
        for template, file, _script in self.install.HOOK_CONFIGS:
            path = self.home / "hooks" / file
            name = f"hook:{file.replace('app-launcher-lite-', '').replace('.json', '')}"
            if not path.is_file():
                checks.append(Check("install", name, FAIL, {"reason": "missing", "path": str(path)}))
                continue
            try:
                installed = self.install.read_text(str(path))
                body = json.loads(installed)
                expected_template = self.install.read_text(str(self.copilot_dir / "hooks" / template))
            except (OSError, ValueError) as exc:
                checks.append(Check("install", name, UNKNOWN, {"reason": f"{type(exc).__name__}: {exc}"}))
                continue
            python = re.search(r"& '([^']+)' '", installed)
            scope = " --lite-sessions-only" if shared and "session-state" in file else ""
            expected = self.install.render_hook_config(
                expected_template, python.group(1) if python else "", str(self.copilot_dir), scope)
            status = PASS if installed.casefold() == expected.casefold() else FAIL
            detail = {"installed": _sha8(installed), "expected": _sha8(expected)}
            if status == FAIL:
                detail["fix"] = "run copilot/install.py"
            checks.append(Check("install", name, status, detail))
            checks.append(self._hook_targets(name, body))
        return checks

    def _hook_targets(self, name: str, body: object) -> Check:
        commands: List[str] = []
        hooks = body.get("hooks") if isinstance(body, dict) else None
        for entries in (hooks.values() if isinstance(hooks, dict) else []):
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict):
                    commands += [v for k, v in entry.items() if k in ("powershell", "bash", "command") and isinstance(v, str)]
        if not commands:
            return Check("install", f"{name}.targets", FAIL, {"reason": "no hook command"})
        targets = sorted({t for c in commands for t in re.findall(r"'([^']+)'", c) if re.search(r"[\\/]", t)})
        missing = [t for t in targets if not os.path.exists(t)]
        if missing:
            return Check("install", f"{name}.targets", FAIL, {"missing": ",".join(missing)})
        return Check("install", f"{name}.targets", PASS, {"files": str(len(targets))})

    def check_launcher(self) -> Check:
        template, file = self.install.CHIEF_LAUNCHER
        path = self.home / "hooks" / file
        if not path.is_file():
            return Check("install", "launcher", FAIL, {"reason": "missing", "fix": "run copilot/install.py"})
        try:
            installed = self.install.read_text(str(path))
            expected_template = self.install.read_text(str(self.copilot_dir / "hooks" / template))
        except OSError as exc:
            return Check("install", "launcher", UNKNOWN, {"reason": f"{type(exc).__name__}: {exc}"})
        if self.install.CHIEF_LAUNCHER_MARKER not in installed:
            return Check("install", "launcher", FAIL, {"reason": "not Lite's launcher"})
        python = re.search(r'^"([^"]+)" "', installed, re.MULTILINE)
        expected = self.install.render_chief_launcher(expected_template, python.group(1) if python else "",
                                                      str(self.copilot_dir))
        if installed.casefold() != expected.casefold():
            return Check("install", "launcher", FAIL, {"reason": "stale or points at another checkout",
                                                       "fix": "run copilot/install.py"})
        try:
            code, out = self.run([str(path), "chief-sid"], VERB_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            return Check("install", "launcher", UNKNOWN, {"reason": f"did not run: {type(exc).__name__}"})
        if not re.search(r"^CHIEF_SID=", out, re.MULTILINE):
            return Check("install", "launcher", FAIL, {"reason": f"no CHIEF_SID line (exit {code})"})
        return Check("install", "launcher", PASS, {"answers": _short(out)})

    def _skills_dir(self) -> Optional[str]:
        try:
            body = json.loads(self.install.read_text(str(self.checkout / "config" / "webapp_config.json")))
        except (OSError, ValueError):
            return None
        value = body.get("skills_dir") if isinstance(body, dict) else None
        return str(value).strip() or None if value else None

    def check_skills(self, shared: bool) -> List[Check]:
        if shared:
            return [Check("install", "skills", PASS, {"mode": "shared", "note": "the full fleet owns the skills folder"})]
        skills_dir = self._skills_dir()
        checks = []
        for source in self.install.list_dirs(str(self.copilot_dir / "skills")):
            name = os.path.basename(source)
            expected = os.path.join(skills_dir, name) if skills_dir and name in self.install.ISSUE_SKILLS else source
            checks.append(self._skill(name, self.home / "skills" / name, expected))
        return checks

    def _skill(self, name: str, entry: Path, expected: str) -> Check:
        label = f"skill:{name}"
        if not os.path.lexists(entry):
            return Check("install", label, FAIL, {"reason": "missing", "fix": "run copilot/install.py"})
        target = self.install.link_target(str(entry))
        if target:
            if self.install.same_path(target, expected):
                return Check("install", label, PASS, {"mode": "junction"})
            return Check("install", label, FAIL, {"reason": "links elsewhere", "target": target})
        if not (entry / self.install.SKILL_MARKER).is_file():
            if name in self.install.ISSUE_SKILLS:
                return Check("install", label, PASS, {"mode": "kept", "note": "not Lite's, counts as the skill"})
            return Check("install", label, UNKNOWN, {"reason": "not Lite's copy, cannot compare"})
        try:
            same = _tree_hash(entry) == _tree_hash(Path(expected))
        except OSError as exc:
            return Check("install", label, UNKNOWN, {"reason": f"{type(exc).__name__}"})
        return Check("install", label, PASS if same else FAIL,
                     {"mode": "copy"} if same else {"mode": "copy", "reason": "stale copy", "fix": "run copilot/install.py"})

    # --- verbs --------------------------------------------------------------------------------

    def verb_specs(self) -> List[Tuple[str, List[str], Callable[[str], Tuple[str, str]]]]:
        def key(prefix: str, unknown: str = "=unknown") -> Callable[[str], Tuple[str, str]]:
            def judge(out: str) -> Tuple[str, str]:
                line = next((ln for ln in out.splitlines() if ln.startswith(prefix)), "")
                if not line:
                    return FAIL, f"no {prefix.strip()} line"
                return (UNKNOWN if unknown in line else PASS), line
            return judge

        def is_json(out: str) -> Tuple[str, str]:
            try:
                json.loads(out)
            except ValueError:
                return FAIL, "not JSON"
            return PASS, "json"

        def issue_state(out: str) -> Tuple[str, str]:
            # Only the ref and its state: an issue title is not for a report that may be shared.
            status, line = key(f"{self.issue_ref}:")(out)
            words = line.split()
            return status, " ".join(words[:2]) if status == PASS else line

        return [
            ("wake", ["wake"], key("WAKE=", "doorbell=unknown")),
            ("inbox-count", ["inbox", "--count"], key("PENDING=")),
            ("board", ["board", "--json"], is_json),
            ("sessions", ["sessions", "--json"], is_json),
            ("chief-sid", ["chief-sid"], key("CHIEF_SID=")),
            ("plan-show", ["plan", "show"], is_json),
            ("slots-idle", ["slots-idle"], key("SLOTS_IDLE=")),
            ("context", ["context"], key("CONTEXT ", "percent=unknown")),
            ("compact-due", ["compact-due"], key("COMPACT_")),
            ("sweep", ["sweep"], key("SUMMARY ")),
            ("issues", ["issues", self.issue_ref], issue_state),
        ]

    def check_verbs(self) -> List[Check]:
        checks = []
        for name, argv, judge in self.verb_specs():
            started = time.monotonic()
            try:
                code, out = self.run([sys.executable, str(CHIEF_OPS), *argv], VERB_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                checks.append(Check("verb", name, UNKNOWN, {"reason": f"timed out after {VERB_TIMEOUT_S}s"}))
                continue
            except (OSError, subprocess.SubprocessError) as exc:
                checks.append(Check("verb", name, UNKNOWN, {"reason": f"did not run: {type(exc).__name__}"}))
                continue
            ms = str(int((time.monotonic() - started) * 1000))
            if re.search(r"^UNREACHABLE=", out, re.MULTILINE):
                checks.append(Check("verb", name, UNKNOWN, {"ms": ms, "reason": "launcher unreachable"}))
                continue
            status, line = judge(out)
            self.verb_lines[name] = line
            checks.append(Check("verb", name, status, {"ms": ms, "exit": str(code), "line": line[:LINE_CHARS]}))
        return checks

    # --- mailbox ------------------------------------------------------------------------------

    def check_mailbox(self) -> Check:
        inbox = mailbox.chief_inbox_dir()
        now_ms = time.time_ns() // 1_000_000
        try:
            pending = mailbox._event_files(inbox)
            claimed = mailbox._event_files(inbox / mailbox.CLAIMED_DIRNAME)
        except OSError as exc:
            return Check("mailbox", "events", UNKNOWN, {"reason": f"unreadable: {type(exc).__name__}"})

        def oldest(paths: List[Path]) -> Optional[int]:
            stamps = [mailbox._split_event_name(p.name)[0] for p in paths]
            known = [s for s in stamps if s is not None]
            return (now_ms - min(known)) // 1000 if known else None

        old_pending, old_claimed = oldest(pending), oldest(claimed)
        detail = {"pending": str(len(pending)), "oldest_pending_s": str(old_pending if old_pending is not None else "-"),
                  "claimed": str(len(claimed)), "oldest_claimed_s": str(old_claimed if old_claimed is not None else "-")}
        if not inbox.is_dir():
            detail["note"] = "no mailbox folder yet"
        problems = []
        if old_pending is not None and old_pending > PENDING_STALE_S:
            problems.append("an event waited past the fallback waiter")
        if old_claimed is not None and old_claimed > mailbox.CLAIM_STALE_SECONDS:
            problems.append("a claim stayed unacked past the stale window")
        if problems:
            detail["reason"] = "; ".join(problems)
        return Check("mailbox", "events", FAIL if problems else PASS, detail)

    def check_doorbell(self) -> List[Check]:
        try:
            config = launcher._request(self.base_url, "/api/config")
        except urllib.error.HTTPError as exc:
            return [Check("mailbox", "doorbell", UNKNOWN, {"reason": f"launcher config HTTP {exc.code}"})]
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            return [Check("launcher", "reachable", FAIL, {"reason": f"{type(exc).__name__}"}),
                    Check("mailbox", "doorbell", UNKNOWN, {"reason": "launcher unreachable"})]
        value = config.get("chief_doorbell") if isinstance(config, dict) else None
        reachable = Check("launcher", "reachable", PASS, {})
        if not isinstance(value, bool):
            return [reachable, Check("mailbox", "doorbell", UNKNOWN, {"reason": "no boolean chief_doorbell"})]
        setting = "on" if value else "off"
        wake = self.verb_lines.get("wake", "")
        said = re.search(r"doorbell=(\w+)", wake)
        if not said or said.group(1) == "unknown":
            return [reachable, Check("mailbox", "doorbell", UNKNOWN, {"setting": setting, "reason": "wake gave no answer"})]
        agree = said.group(1) == setting
        return [reachable, Check("mailbox", "doorbell", PASS if agree else FAIL,
                                 {"setting": setting, "wake": said.group(1)})]

    # --- logs ---------------------------------------------------------------------------------

    def check_logs(self) -> Tuple[List[Check], List[doctor_logs.Group]]:
        checks, groups = [], []
        sources = (("launcher", lambda: doctor_logs.scan_lite_logs(self.checkout / "webapp", datetime.now())),
                   ("copilot", lambda: doctor_logs.scan_copilot_logs(self.home / "logs", datetime.now(timezone.utc))))
        for name, scan in sources:
            try:
                found = scan()
            except OSError as exc:
                checks.append(Check("logs", name, UNKNOWN, {"reason": f"unreadable: {type(exc).__name__}"}))
                continue
            groups += found
            fails = sum(g.count for g in found if g.severity == doctor_logs.FAIL)
            notes = sum(g.count for g in found if g.severity == doctor_logs.NOTE)
            checks.append(Check("logs", name, FAIL if fails else PASS,
                                {"failures": str(fails), "notes": str(notes), "window": "24h"}))
        return checks, groups

    # --- the run ------------------------------------------------------------------------------

    def run_all(self) -> Tuple[List[Check], List[doctor_logs.Group]]:
        shared = self.shared_machine()
        checks = [self.check_commit(), self.check_copilot_cli(), self.check_venv()]
        checks += self.check_hooks(shared)
        checks.append(self.check_launcher())
        checks += self.check_skills(shared)
        checks += self.check_verbs()
        checks.append(self.check_mailbox())
        checks += self.check_doorbell()
        log_checks, groups = self.check_logs()
        return checks + log_checks, groups


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if path.name == ".app-launcher-lite-managed" or "__pycache__" in rel:
            continue
        digest.update(rel.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _local(moment: datetime) -> datetime:
    """Copilot logs in UTC and Lite in local time; the report shows both in local time."""
    return moment.astimezone() if moment.tzinfo else moment


def verdict(checks: List[Check]) -> str:
    fails = sum(c.status == FAIL for c in checks)
    unknowns = sum(c.status == UNKNOWN for c in checks)
    overall = FAIL if fails else UNKNOWN if unknowns else PASS
    return f"DOCTOR={overall} fails={fails} unknowns={unknowns}"


def render(checks: List[Check], groups: List[doctor_logs.Group], redact: Callable[[str], str]) -> List[str]:
    lines = [redact(c.line()) for c in checks]
    shown = sorted(groups, key=lambda g: (g.severity != doctor_logs.FAIL, -g.count))[:LOG_GROUPS_SHOWN]
    for g in shown:
        lines.append(redact(f'LOG source={g.source} kind={g.kind} severity={g.severity} count={g.count} '
                            f'last={_local(g.last).strftime("%Y-%m-%dT%H:%M")} msg="{g.message.replace(chr(34), chr(39))}"'))
    if len(groups) > len(shown):
        lines.append(f"LOG_GROUPS_NOT_SHOWN={len(groups) - len(shown)}")
    lines.append(verdict(checks))
    return lines


def cmd_doctor(args: argparse.Namespace) -> int:
    checkout = Path(args.checkout).resolve() if args.checkout else DEFAULT_CHECKOUT
    home = hooks_state.copilot_home()
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    doctor = Doctor(checkout, home, profile, base_url=args.base_url, issue_ref=args.issue_ref)
    checks, groups = doctor.run_all()
    lines = render(checks, groups, Redactor.for_machine(checkout, home))
    print("\n".join(lines))
    return 0 if lines[-1].startswith(f"DOCTOR={PASS} ") else 1


def add_parser(sub: argparse._SubParsersAction) -> None:
    dr = sub.add_parser("doctor", help="read-only check of this machine's chief setup (KEY=value lines)")
    dr.add_argument("--checkout", default=None, help="the Lite checkout the install should match (default: this one)")
    dr.add_argument("--issue-ref", default=None, help="the <repo>#<N> the issues verb reads (default: <checkout>#1)")
    dr.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    dr.set_defaults(func=cmd_doctor)
