"""``copilot/install.ps1``: install, ``-Uninstall`` and ``-Migrate`` against a temp profile.

Each case copies the installer, the hook templates, one fake skill and the ports
reader into a temp checkout and points ``COPILOT_HOME`` and ``USERPROFILE`` at a
temp folder, so nothing in the real user profile is read or written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from src.launching.ps_script import script_command
from src.subprocess_flags import NO_WINDOW

pytestmark = [
    pytest.mark.windows_only,
    pytest.mark.skipif(sys.platform != "win32", reason="the installer is PowerShell and the hazard is an NTFS junction"),
]

_COPILOT = Path(__file__).resolve().parents[3] / "copilot"
_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_STATE_FILE = "sessions-state.json"
_STATE_BODY = '{"rows": [{"sid": "keep-me"}]}'


def _ps(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW,
    )


def _remove_links(root: Path) -> None:
    """Delete every junction under ``root`` as a link, so the temp tree's cleanup never walks into a target."""
    for dirpath, dirnames, _files in os.walk(root):
        for name in list(dirnames):
            path = Path(dirpath) / name
            if path.is_junction() or path.is_symlink():
                os.rmdir(path)
                dirnames.remove(name)


class _Settings(BaseHTTPRequestHandler):
    """The two Settings routes the installer uses, over one remembered ``fleet_config_dir``."""

    def _reply(self, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 -- http.server's name
        self._reply({"fleet_config_dir": self.server.fleet_config_dir, "projects_dir": "untouched"})  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 -- http.server's name
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.posted.append(body)  # type: ignore[attr-defined]
        self.server.fleet_config_dir = body.get("fleet_config_dir", self.server.fleet_config_dir)  # type: ignore[attr-defined]
        self._reply({"ok": True})

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def lite(tmp_path: Path) -> Iterator["Lite"]:
    box = Lite(tmp_path)
    yield box
    _remove_links(tmp_path)


class Lite:
    """A temp Lite checkout plus a temp Copilot home."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "lite"
        self.copilot = self.repo / "copilot"
        (self.copilot / "hooks").mkdir(parents=True)
        (self.copilot / "skills" / "_lib").mkdir(parents=True)
        (self.repo / "config").mkdir()
        (self.copilot / "install.ps1").write_bytes((_COPILOT / "install.ps1").read_bytes())
        for template in (_COPILOT / "hooks").glob("*.template.json"):
            (self.copilot / "hooks" / template.name).write_bytes(template.read_bytes())
        ports = _COPILOT / "skills" / "_lib" / "lite_ports.py"
        (self.copilot / "skills" / "_lib" / "lite_ports.py").write_bytes(ports.read_bytes())
        (self.copilot / "skills" / "demo").mkdir()
        (self.copilot / "skills" / "demo" / "SKILL.md").write_text("demo", encoding="utf-8")
        self.profile = root / "profile"
        self.home = self.profile / ".copilot"
        self.home.mkdir(parents=True)

    @property
    def hooks(self) -> Path:
        return self.home / "hooks"

    @property
    def skills(self) -> Path:
        return self.home / "skills"

    def run(self, *flags: str, answer: Optional[str] = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "COPILOT_HOME": str(self.home), "USERPROFILE": str(self.profile)}
        return subprocess.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.copilot / "install.ps1"),
             "-SkipChecklist", *flags],
            capture_output=True, text=True, timeout=180, env=env, input=answer or "", creationflags=NO_WINDOW,
        )

    def ok(self, *flags: str) -> str:
        proc = self.run(*flags)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    def junction(self, link: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        made = _ps(f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null")
        assert made.returncode == 0, made.stderr

    def seed_state(self) -> Path:
        state = self.hooks / "state" / _STATE_FILE
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(_STATE_BODY, encoding="utf-8")
        return state

    def seed_old_install(self) -> List[Path]:
        """What the old two-repo install left: its two hook configs and a skill junction into its checkout."""
        old_repo = self.root / "fleet-config-lite"
        self.hooks.mkdir(parents=True, exist_ok=True)
        old_hooks = []
        for name, script in (("session-state", "session_state.py"), ("chief-guard", "chief_guard.py")):
            path = self.hooks / f"fleet-config-lite-{name}.json"
            path.write_text(f'{{"powershell": "& python \'{old_repo.as_posix()}/hooks/{script}\'"}}', encoding="utf-8")
            old_hooks.append(path)
        self.junction(self.skills / "demo", old_repo / "skills" / "demo")
        return old_hooks

    def serve_settings(self, fleet_config_dir: str) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Settings)
        server.fleet_config_dir = fleet_config_dir  # type: ignore[attr-defined]
        server.posted = []  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (self.repo / "config" / "config.json").write_text(
            json.dumps({"webapp": {"port": server.server_address[1]}}), encoding="utf-8")
        return server

    def set_config(self, fleet_config_dir: Optional[str]) -> None:
        body = {} if fleet_config_dir is None else {"fleet_config_dir": fleet_config_dir}
        (self.repo / "config" / "webapp_config.json").write_text(json.dumps(body), encoding="utf-8")


def test_install_renders_this_checkouts_hooks_and_links_skills_and_a_rerun_changes_nothing(lite: Lite) -> None:
    state = lite.seed_state()
    lite.ok()
    first = {p.name: p.read_bytes() for p in lite.hooks.glob("app-launcher-lite-*.json")}
    assert set(first) == {"app-launcher-lite-session-state.json", "app-launcher-lite-chief-guard.json"}
    for body in first.values():
        text = body.decode("utf-8")
        assert not body.startswith(b"\xef\xbb\xbf") and "{{" not in text
        assert (lite.copilot.as_posix() + "/hooks/") in text
    assert "--lite-sessions-only" not in first["app-launcher-lite-session-state.json"].decode("utf-8")
    assert (lite.skills / "demo").is_junction()
    lite.ok()
    assert {p.name: p.read_bytes() for p in lite.hooks.glob("app-launcher-lite-*.json")} == first
    assert (lite.skills / "demo").is_junction()
    assert state.read_text(encoding="utf-8") == _STATE_BODY


def test_uninstall_removes_only_what_is_ours_and_is_idempotent(lite: Lite) -> None:
    state = lite.seed_state()
    lite.ok()
    foreign_hook = lite.hooks / "someone-elses.json"
    foreign_hook.write_text("{}", encoding="utf-8")
    foreign_skill = lite.skills / "their-skill"
    foreign_skill.mkdir()
    (foreign_skill / "SKILL.md").write_text("theirs", encoding="utf-8")
    other_target = lite.root / "elsewhere" / "linked"
    lite.junction(lite.skills / "linked", other_target)
    (other_target / "keep.txt").write_text("keep", encoding="utf-8")

    for _ in range(2):
        lite.ok("-Uninstall")
    assert not list(lite.hooks.glob("app-launcher-lite-*.json"))
    assert not (lite.skills / "demo").exists() and not (lite.skills / "_lib").exists()
    assert foreign_hook.exists() and (foreign_skill / "SKILL.md").read_text(encoding="utf-8") == "theirs"
    assert (lite.skills / "linked").is_junction() and (other_target / "keep.txt").exists()
    assert state.read_text(encoding="utf-8") == _STATE_BODY
    assert (lite.copilot / "skills" / "demo" / "SKILL.md").exists(), "uninstall must never follow a link into the repo"


def test_uninstall_leaves_a_hook_config_that_does_not_run_this_checkout(lite: Lite) -> None:
    lite.hooks.mkdir(parents=True)
    imposter = lite.hooks / "app-launcher-lite-chief-guard.json"
    imposter.write_text('{"powershell": "& python C:/other/hooks/chief_guard.py"}', encoding="utf-8")
    out = lite.ok("-Uninstall")
    assert imposter.exists() and "[skip] hook config is not ours" in out


def test_migrate_removes_the_old_install_installs_the_new_and_keeps_state(lite: Lite) -> None:
    state = lite.seed_state()
    old_hooks = lite.seed_old_install()
    foreign = lite.root / "elsewhere" / "linked"
    lite.junction(lite.skills / "linked", foreign)

    out = lite.ok("-Migrate", "-Yes")
    assert not any(p.exists() for p in old_hooks)
    assert (lite.hooks / "app-launcher-lite-session-state.json").exists()
    assert (lite.skills / "demo").is_junction() and (lite.copilot / "skills" / "demo" / "SKILL.md").exists()
    assert (lite.skills / "linked").is_junction(), "a link that is not the old install's is never removed"
    assert (lite.root / "fleet-config-lite" / "skills" / "demo").exists(), "the old checkout itself stays"
    assert state.read_text(encoding="utf-8") == _STATE_BODY
    assert "[ok] old hook config removed" in out

    again = lite.ok("-Migrate", "-Yes")
    assert "[ok] no fleet-config-lite hook config or skill link to remove" in again
    assert state.read_text(encoding="utf-8") == _STATE_BODY


@pytest.mark.parametrize("answer", [None, "n\n", "\n"])
def test_migrate_asks_first_and_no_answer_or_a_no_changes_nothing(lite: Lite, answer: Optional[str]) -> None:
    old_hooks = lite.seed_old_install()
    proc = lite.run("-Migrate", answer=answer)  # None: stdin is closed, so there is nobody to answer
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "migration declined: nothing was changed" in proc.stdout
    assert all(p.exists() for p in old_hooks)
    assert not list(lite.hooks.glob("app-launcher-lite-*.json"))


def test_migrate_goes_ahead_on_a_typed_yes(lite: Lite) -> None:
    old_hooks = lite.seed_old_install()
    proc = lite.run("-Migrate", answer="y\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(p.exists() for p in old_hooks)
    assert (lite.hooks / "app-launcher-lite-chief-guard.json").exists()


def test_a_plain_install_beside_the_old_hooks_warns_that_two_session_state_hooks_race(lite: Lite) -> None:
    old_hooks = lite.seed_old_install()
    out = lite.ok()
    assert "[warn] the old fleet-config-lite hook configs are still installed" in out
    assert all(p.exists() for p in old_hooks)


def test_migrate_repoints_an_explicit_copilot_folder_through_the_settings_api_and_reads_it_back(lite: Lite) -> None:
    server = lite.serve_settings(r"C:\old\fleet-config-lite")
    lite.set_config(r"C:\old\fleet-config-lite")
    try:
        out = lite.ok("-Migrate", "-Yes")
    finally:
        server.shutdown()
    assert server.posted == [{"fleet_config_dir": str(lite.copilot)}], "exactly one field is sent"  # type: ignore[attr-defined]
    assert "[ok] Copilot folder setting ->" in out and "read back from Lite" in out


@pytest.mark.parametrize("current", [None, "", "{copilot}"])
def test_migrate_leaves_an_empty_or_already_correct_copilot_folder_alone(lite: Lite, current: Optional[str]) -> None:
    server = lite.serve_settings("")
    lite.set_config(None if current is None else current.format(copilot=lite.copilot))
    try:
        lite.ok("-Migrate", "-Yes")
    finally:
        server.shutdown()
    assert server.posted == []  # type: ignore[attr-defined]


def test_migrate_reports_unknown_when_lite_is_not_running_and_never_edits_the_config_file(lite: Lite) -> None:
    lite.set_config(r"C:\old\fleet-config-lite")
    (lite.repo / "config" / "config.json").write_text('{"webapp": {"port": 9}}', encoding="utf-8")
    config = lite.repo / "config" / "webapp_config.json"
    before = config.read_bytes()
    out = lite.ok("-Migrate", "-Yes")
    assert "[unknown] Lite did not take the change" in out
    assert config.read_bytes() == before


def test_a_shared_machine_gets_no_skills_and_a_lite_only_session_hook(lite: Lite) -> None:
    other = lite.root / "full-fleet" / "skills"
    lite.junction(lite.profile / ".agents" / "skills", other)
    out = lite.ok()
    assert "[note] shared machine" in out and "[skip] skills not installed on a shared machine" in out
    assert "--lite-sessions-only" in (lite.hooks / "app-launcher-lite-session-state.json").read_text(encoding="utf-8")
    assert not (lite.skills / "demo").exists() and list(other.iterdir()) == []


def test_a_junctioned_skills_dir_is_never_written_or_removed_through(lite: Lite) -> None:
    other = lite.root / "full-fleet" / "skills"
    (other / "theirs").mkdir(parents=True)
    lite.junction(lite.home / "skills", other)
    lite.ok()
    lite.ok("-Uninstall")
    assert [p.name for p in other.iterdir()] == ["theirs"]


def test_install_and_migrate_run_under_allsigned_through_the_scriptblock_launcher(lite: Lite) -> None:
    # An AllSigned machine refuses `-File` for this unsigned script; ps_script loads it as text instead,
    # which also rewrites $PSScriptRoot and starts PowerShell -NonInteractive.
    old_hooks = lite.seed_old_install()
    argv, extra = script_command(lite.copilot / "install.ps1", ["-SkipChecklist", "-Migrate", "-Yes"], policy="allsigned")
    env = {**os.environ, "COPILOT_HOME": str(lite.home), "USERPROFILE": str(lite.profile), **extra}
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=180, env=env, stdin=subprocess.DEVNULL,
                          creationflags=NO_WINDOW)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(p.exists() for p in old_hooks)
    assert (lite.hooks / "app-launcher-lite-session-state.json").exists() and (lite.skills / "demo").is_junction()


def test_a_link_into_a_sibling_checkout_whose_name_extends_this_one_is_still_foreign(lite: Lite) -> None:
    sibling = lite.root / "lite-wt-1" / "skills"
    lite.junction(lite.home / "skills", sibling)
    out = lite.ok()
    assert "[note] shared machine" in out and list(sibling.iterdir()) == []
