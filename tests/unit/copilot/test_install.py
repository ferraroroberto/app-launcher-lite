"""``copilot/install.py``: install, ``--uninstall`` and ``--migrate`` against a temp profile.

Each case copies the installer, its PowerShell wrapper, the hook templates, one fake skill
and the ports reader into a temp checkout and points ``COPILOT_HOME`` and ``USERPROFILE`` at
a temp folder, so nothing in the real user profile is read or written. The cases run the
Python entry point with no PowerShell at all, the path a machine that only allows signed
scripts takes; one case checks that ``install.ps1`` only forwards to it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import venv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

if sys.platform == "win32":
    import _winapi

from src.subprocess_flags import NO_WINDOW

pytestmark = [
    pytest.mark.windows_only,
    pytest.mark.skipif(sys.platform != "win32", reason="the installer writes NTFS junctions"),
]

_COPILOT = Path(__file__).resolve().parents[3] / "copilot"
_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_STATE_FILE = "sessions-state.json"
_STATE_BODY = '{"rows": [{"sid": "keep-me"}]}'
_ISSUE_SKILLS = ("issue-add", "issue-start", "issue-yolo", "issue-finish")


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
        for name in ("install.py", "install_state.py", "install.ps1", "install.bat"):
            (self.copilot / name).write_bytes((_COPILOT / name).read_bytes())
        for template in (_COPILOT / "hooks").glob("*.template.json"):
            (self.copilot / "hooks" / template.name).write_bytes(template.read_bytes())
        (self.copilot / "hooks" / "chief-ops.template.cmd").write_bytes(
            (_COPILOT / "hooks" / "chief-ops.template.cmd").read_bytes())
        (self.copilot / "skills" / "_lib" / "chief_ops.py").write_text(
            "import os, sys\nprint('ARGS=' + ' '.join(sys.argv[1:]))\nprint('CWD=' + os.getcwd())\n", encoding="utf-8")
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

    @property
    def state(self) -> Path:
        return self.home / "app-launcher-lite-state"

    def run(self, *flags: str, answer: Optional[str] = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "COPILOT_HOME": str(self.home), "USERPROFILE": str(self.profile)}
        return subprocess.run(
            [sys.executable, str(self.copilot / "install.py"), "--skip-checklist", *flags],
            capture_output=True, text=True, timeout=180, env=env, input=answer or "", creationflags=NO_WINDOW,
        )

    def env(self) -> dict:
        return {**os.environ, "COPILOT_HOME": str(self.home), "USERPROFILE": str(self.profile)}

    def run_bat(self, *flags: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
        """``install.bat`` started straight from the checkout's ``copilot`` folder, the way a person would."""
        return subprocess.run(
            [str(self.copilot / "install.bat"), "--skip-checklist", *flags],
            capture_output=True, text=True, timeout=180, env=env or self.env(), stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )

    def ok(self, *flags: str) -> str:
        proc = self.run(*flags)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    def junction(self, link: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(target), str(link))

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

    def seed_bundled_issue_skills(self) -> None:
        for name in _ISSUE_SKILLS:
            (self.copilot / "skills" / name).mkdir()
            (self.copilot / "skills" / name / "SKILL.md").write_text("bundled", encoding="utf-8")

    def own_skills_repo(self) -> Path:
        """A skills repo of the machine's own, holding the four issue skills."""
        own = self.root / "own-skills"
        for name in _ISSUE_SKILLS:
            (own / name).mkdir(parents=True)
            (own / name / "SKILL.md").write_text(f"own {name}", encoding="utf-8")
        return own

    def write_config(self, body: dict) -> Path:
        path = self.repo / "config" / "webapp_config.json"
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        return path

    def target(self, link: Path) -> Path:
        # os.readlink keeps the junction's \\?\ long-path prefix.
        return Path(os.readlink(link).removeprefix("\\\\?\\")).resolve()

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
        lite.ok("--uninstall")
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
    out = lite.ok("--uninstall")
    assert imposter.exists() and "[skip] hook config is not ours" in out


def test_migrate_removes_the_old_install_installs_the_new_and_keeps_state(lite: Lite) -> None:
    state = lite.seed_state()
    old_hooks = lite.seed_old_install()
    foreign = lite.root / "elsewhere" / "linked"
    lite.junction(lite.skills / "linked", foreign)

    out = lite.ok("--migrate", "--yes")
    assert not any(p.exists() for p in old_hooks)
    assert (lite.hooks / "app-launcher-lite-session-state.json").exists()
    assert (lite.skills / "demo").is_junction() and (lite.copilot / "skills" / "demo" / "SKILL.md").exists()
    assert (lite.skills / "linked").is_junction(), "a link that is not the old install's is never removed"
    assert (lite.root / "fleet-config-lite" / "skills" / "demo").exists(), "the old checkout itself stays"
    assert not state.exists() and (lite.state / _STATE_FILE).read_text(encoding="utf-8") == _STATE_BODY
    assert "[ok] old hook config removed" in out

    again = lite.ok("--migrate", "--yes")
    assert "[ok] no fleet-config-lite hook config or skill link to remove" in again
    assert (lite.state / _STATE_FILE).read_text(encoding="utf-8") == _STATE_BODY


@pytest.mark.parametrize("answer", [None, "n\n", "\n"])
def test_migrate_asks_first_and_no_answer_or_a_no_changes_nothing(lite: Lite, answer: Optional[str]) -> None:
    old_hooks = lite.seed_old_install()
    proc = lite.run("--migrate", answer=answer)  # None: stdin is closed, so there is nobody to answer
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "migration declined: nothing was changed" in proc.stdout
    assert all(p.exists() for p in old_hooks)
    assert not list(lite.hooks.glob("app-launcher-lite-*.json"))


def test_migrate_goes_ahead_on_a_typed_yes(lite: Lite) -> None:
    old_hooks = lite.seed_old_install()
    proc = lite.run("--migrate", answer="y\n")
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
        out = lite.ok("--migrate", "--yes")
    finally:
        server.shutdown()
    assert server.posted == [{"fleet_config_dir": str(lite.copilot)}], "exactly one field is sent"  # type: ignore[attr-defined]
    assert "[ok] Copilot folder setting ->" in out and "read back from Lite" in out


@pytest.mark.parametrize("current", [None, "", "{copilot}"])
def test_migrate_leaves_an_empty_or_already_correct_copilot_folder_alone(lite: Lite, current: Optional[str]) -> None:
    server = lite.serve_settings("")
    lite.set_config(None if current is None else current.format(copilot=lite.copilot))
    try:
        lite.ok("--migrate", "--yes")
    finally:
        server.shutdown()
    assert server.posted == []  # type: ignore[attr-defined]


def test_migrate_reports_unknown_when_lite_is_not_running_and_never_edits_the_config_file(lite: Lite) -> None:
    lite.set_config(r"C:\old\fleet-config-lite")
    (lite.repo / "config" / "config.json").write_text('{"webapp": {"port": 9}}', encoding="utf-8")
    config = lite.repo / "config" / "webapp_config.json"
    out = lite.ok("--migrate", "--yes")
    assert "[unknown] Lite did not take the change" in out
    # The folder setting is never rewritten in the file; only the missing skills_dir key is added.
    assert json.loads(config.read_text(encoding="utf-8")) == {"skills_dir": "", "fleet_config_dir": r"C:\old\fleet-config-lite"}


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
    lite.ok("--uninstall")
    assert [p.name for p in other.iterdir()] == ["theirs"]


def test_install_ps1_only_forwards_its_switches_to_install_py(lite: Lite) -> None:
    old_hooks = lite.seed_old_install()
    env = {**os.environ, "COPILOT_HOME": str(lite.home), "USERPROFILE": str(lite.profile)}
    proc = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(lite.copilot / "install.ps1"),
         "-SkipChecklist", "-Migrate", "-Yes"],
        capture_output=True, text=True, timeout=180, env=env, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(p.exists() for p in old_hooks)
    assert (lite.hooks / "app-launcher-lite-session-state.json").exists() and (lite.skills / "demo").is_junction()


def test_install_bat_forwards_every_argument_unchanged_and_returns_the_exit_code(tmp_path: Path) -> None:
    """A stub ``install.py`` records the argv it received, so the batch file's own quoting is what is under test."""
    copilot = tmp_path / "repo" / "copilot"
    copilot.mkdir(parents=True)
    (copilot / "install.bat").write_bytes((_COPILOT / "install.bat").read_bytes())
    seen = tmp_path / "argv.json"
    (copilot / "install.py").write_text(
        "import json, os, sys\n"
        "json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, open(os.environ['ARGV_OUT'], 'w'))\n"
        "sys.exit(7)\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    args = ["--migrate", "--yes", "--what-if", "two words", "--flag=a b"]
    proc = subprocess.run(
        [str(copilot / "install.bat"), *args], capture_output=True, text=True, timeout=60, cwd=elsewhere,
        env={**os.environ, "ARGV_OUT": str(seen)}, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )
    assert proc.returncode == 7, proc.stdout + proc.stderr
    got = json.loads(seen.read_text(encoding="utf-8"))
    assert got["argv"] == args
    assert Path(got["cwd"]).resolve() == (tmp_path / "repo").resolve(), "the batch file changes to the repository root"


def test_install_bat_prefers_the_repo_venv_and_says_so_when_there_is_no_python(tmp_path: Path) -> None:
    """A stub ``install.py`` reports which interpreter ran it, so only the batch file's choice is under test."""
    copilot = tmp_path / "repo" / "copilot"
    copilot.mkdir(parents=True)
    (copilot / "install.bat").write_bytes((_COPILOT / "install.bat").read_bytes())
    (copilot / "install.py").write_text("import sys\nprint(sys.prefix)\n", encoding="utf-8")
    # Only System32 on PATH: no py launcher and no python, so a pass can only come from the repo's own venv.
    bare = {**os.environ, "PATH": r"C:\Windows\System32"}

    def run() -> subprocess.CompletedProcess:
        return subprocess.run([str(copilot / "install.bat")], capture_output=True, text=True, timeout=60, env=bare,
                              stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)

    missing = run()
    assert missing.returncode == 1 and "no Python found" in missing.stdout, missing.stdout + missing.stderr
    venv.create(tmp_path / "repo" / ".venv", with_pip=False)
    proc = run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert Path(proc.stdout.strip()).resolve() == (tmp_path / "repo" / ".venv").resolve()


def test_install_bat_migrates_like_install_py(lite: Lite) -> None:
    state = lite.seed_state()
    old_hooks = lite.seed_old_install()
    proc = lite.run_bat("--migrate", "--yes")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(p.exists() for p in old_hooks)
    assert (lite.hooks / "app-launcher-lite-session-state.json").exists()
    assert (lite.hooks / "app-launcher-lite-chief-guard.json").exists()
    assert (lite.skills / "demo").is_junction() and (lite.copilot / "skills" / "demo" / "SKILL.md").exists()
    assert not state.exists() and (lite.state / _STATE_FILE).read_text(encoding="utf-8") == _STATE_BODY


def test_install_bat_runs_from_powershell_under_allsigned_and_passes_the_exit_code_back(lite: Lite) -> None:
    """The per-process flag only: the machine's and the user's policy are never touched."""
    old_hooks = lite.seed_old_install()
    roots = (lite.profile, lite.repo / "config")
    before = _snapshot(*roots)

    def under_allsigned(*flags: str) -> subprocess.CompletedProcess:
        command = f"& '{lite.copilot / 'install.bat'}' {' '.join(flags)}; exit $LASTEXITCODE"
        return subprocess.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "AllSigned", "-Command", command],
            capture_output=True, text=True, timeout=180, env=lite.env(), stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )

    blocked = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "AllSigned", "-File", str(lite.copilot / "install.ps1"),
         "-SkipChecklist", "-WhatIf"],
        capture_output=True, text=True, timeout=180, env=lite.env(), stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    assert blocked.returncode != 0, "AllSigned must refuse the unsigned .ps1, or this test proves nothing"

    proc = under_allsigned("--what-if", "--skip-checklist")
    assert proc.returncode == 0 and "WhatIf: nothing was changed" in proc.stdout, proc.stdout + proc.stderr
    assert _snapshot(*roots) == before and all(p.exists() for p in old_hooks)

    declined = under_allsigned("--migrate", "--skip-checklist")  # stdin closed: nobody to confirm
    assert declined.returncode == 1 and "migration declined" in declined.stdout, declined.stdout + declined.stderr


def test_a_link_into_a_sibling_checkout_whose_name_extends_this_one_is_still_foreign(lite: Lite) -> None:
    sibling = lite.root / "lite-wt-1" / "skills"
    lite.junction(lite.home / "skills", sibling)
    out = lite.ok()
    assert "[note] shared machine" in out and list(sibling.iterdir()) == []


def _snapshot(*roots: Path) -> dict:
    """Every file's bytes and every link's target under ``roots``, links not followed."""
    seen: dict = {}
    for root in roots:
        for dirpath, dirnames, files in os.walk(root):
            for name in list(dirnames):
                path = Path(dirpath) / name
                if path.is_junction() or path.is_symlink():
                    seen[str(path)] = ("link", os.readlink(path))
                    dirnames.remove(name)
            for name in files:
                path = Path(dirpath) / name
                seen[str(path)] = ("file", path.read_bytes())
    return seen


def _seed_foreign(lite: Lite) -> tuple:
    """A foreign issue skill in ~/.copilot/skills, a foreign skill in a real ~/.claude/skills."""
    lite.seed_bundled_issue_skills()
    foreign = lite.skills / "issue-add"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("theirs", encoding="utf-8")
    claude = lite.profile / ".claude" / "skills"
    (claude / "their-skill").mkdir(parents=True)
    (claude / "their-skill" / "SKILL.md").write_text("theirs", encoding="utf-8")
    return foreign, claude


def test_skills_dir_links_the_four_from_that_folder_keeps_foreign_entries_and_a_rerun_is_unchanged(lite: Lite) -> None:
    foreign, claude = _seed_foreign(lite)
    own = lite.own_skills_repo()
    config = lite.write_config({"skills_dir": str(own), "gitlab_host": "git.example.test"})
    before_config = config.read_bytes()

    out = lite.ok()
    for name in ("issue-start", "issue-yolo", "issue-finish"):
        assert lite.target(lite.skills / name) == (own / name).resolve(), name
        assert lite.target(claude / name) == (own / name).resolve(), name
    # Not Lite's: kept as it was, and reported as satisfying the skill.
    assert not foreign.is_junction() and (foreign / "SKILL.md").read_text(encoding="utf-8") == "theirs"
    assert "[ok] kept (not Lite's), and it counts as issue-add" in out
    assert lite.target(claude / "issue-add") == (own / "issue-add").resolve()
    assert (lite.skills / "demo").is_junction(), "Lite's other skills install as before"
    assert (claude / "their-skill" / "SKILL.md").read_text(encoding="utf-8") == "theirs"
    assert not (claude / "demo").exists(), "only the four issue skills go into ~/.claude/skills"
    assert config.read_bytes() == before_config and "[ok] unchanged: skills_dir is already in webapp_config.json" in out
    record = json.loads((lite.home / "app-launcher-lite-install.json").read_text(encoding="utf-8"))
    assert len(record["skill_links"]) == 7

    snapshot = _snapshot(lite.profile, lite.repo / "config")
    again = lite.ok()
    assert _snapshot(lite.profile, lite.repo / "config") == snapshot
    assert "[ok] issue skill linked" not in again and "[ok] hook config ->" not in again
    assert again.count("[ok] unchanged ->") >= 10, again


def test_clearing_skills_dir_puts_the_bundled_skills_back_and_drops_the_record(lite: Lite) -> None:
    lite.seed_bundled_issue_skills()
    own = lite.own_skills_repo()
    lite.write_config({"skills_dir": str(own)})
    lite.ok()
    lite.write_config({"skills_dir": ""})
    lite.ok()
    for name in _ISSUE_SKILLS:
        assert lite.target(lite.skills / name) == (lite.copilot / "skills" / name).resolve(), name
    assert not (lite.home / "app-launcher-lite-install.json").exists()
    assert all((own / name / "SKILL.md").exists() for name in _ISSUE_SKILLS)


def test_a_missing_key_is_added_after_a_backup_and_no_value_is_overwritten(lite: Lite) -> None:
    body = {"fleet_config_dir": "D:/somewhere", "gitlab_group": "team", "copilot_models": ["a", "b"]}
    config = lite.write_config(body)
    original = config.read_bytes()
    out = lite.ok()
    assert json.loads(config.read_text(encoding="utf-8")) == {**body, "skills_dir": ""}
    backups = list(config.parent.glob("webapp_config.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert "[ok] key added to webapp_config.json: skills_dir" in out


def test_a_config_that_does_not_parse_is_left_untouched_and_reported(lite: Lite) -> None:
    config = lite.repo / "config" / "webapp_config.json"
    config.write_text('{"fleet_config_dir": ', encoding="utf-8")
    out = lite.ok()
    assert config.read_text(encoding="utf-8") == '{"fleet_config_dir": '
    assert "[unknown] webapp_config.json did not parse" in out
    assert not list(config.parent.glob("webapp_config.json.bak-*"))


def test_whatif_lists_every_change_and_makes_none(lite: Lite) -> None:
    _seed_foreign(lite)
    own = lite.own_skills_repo()
    lite.seed_state()
    lite.write_config({"gitlab_host": "git.example.test"})
    roots = (lite.profile, lite.repo / "config")
    before = _snapshot(*roots)
    out = lite.ok("--what-if")
    assert _snapshot(*roots) == before
    for line in ("[whatif] hook config ->", "[whatif] skill junction ->", "[whatif] backup written ->",
                 "[whatif] key added to webapp_config.json: skills_dir", "WhatIf: nothing was changed"):
        assert line in out, line

    # Installed with skills_dir set, -WhatIf names the swap back and the uninstall the same way.
    lite.write_config({"skills_dir": str(own)})
    lite.ok()
    lite.write_config({"skills_dir": ""})
    before = _snapshot(*roots)
    out = lite.ok("--what-if")
    assert "[whatif] skills_dir link removed ->" in out
    out = lite.ok("--uninstall", "--what-if")
    assert "[whatif] hook config removed ->" in out and "[whatif] install record removed" in out
    assert _snapshot(*roots) == before


def test_uninstall_removes_only_its_own_skills_dir_links_and_keeps_everything_foreign(lite: Lite) -> None:
    foreign, claude = _seed_foreign(lite)
    state = lite.seed_state()
    own = lite.own_skills_repo()
    lite.write_config({"skills_dir": str(own)})
    lite.ok()
    # The owner repoints one recorded link by hand: it is no longer Lite's.
    elsewhere = lite.root / "elsewhere" / "issue-yolo"
    os.rmdir(lite.skills / "issue-yolo")
    lite.junction(lite.skills / "issue-yolo", elsewhere)

    lite.ok("--uninstall")
    for name in ("issue-start", "issue-finish"):
        assert not (lite.skills / name).exists() and not (claude / name).exists(), name
    assert lite.target(lite.skills / "issue-yolo") == elsewhere.resolve()
    assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "theirs"
    assert (claude / "their-skill" / "SKILL.md").exists()
    assert not (lite.home / "app-launcher-lite-install.json").exists()
    assert all((own / name / "SKILL.md").exists() for name in _ISSUE_SKILLS), "never followed into the skills repo"
    assert state.read_text(encoding="utf-8") == _STATE_BODY


def test_skills_dir_never_writes_inside_a_skills_folder_that_links_into_another_repo(lite: Lite) -> None:
    own = lite.own_skills_repo()
    lite.write_config({"skills_dir": str(own)})
    other = lite.root / "full-fleet" / "skills"
    (other / "theirs").mkdir(parents=True)
    lite.junction(lite.home / "skills", other)
    claude_other = lite.root / "other-claude" / "skills"
    lite.junction(lite.profile / ".claude" / "skills", claude_other)
    lite.ok()
    assert [p.name for p in other.iterdir()] == ["theirs"]
    assert list(claude_other.iterdir()) == []


def test_the_chief_launcher_runs_from_any_working_directory_and_is_removed_by_uninstall(lite: Lite, tmp_path: Path) -> None:
    lite.ok()
    launcher = lite.hooks / "app-launcher-lite-chief-ops.cmd"
    assert launcher.is_file()
    elsewhere = tmp_path / "somewhere else"
    elsewhere.mkdir()
    proc = subprocess.run(
        [str(launcher), "inbox", "--count"], cwd=elsewhere, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ARGS=inbox --count" in proc.stdout
    assert f"CWD={elsewhere}" in proc.stdout
    assert "{{" not in launcher.read_text(encoding="utf-8")
    assert f"[ok] unchanged -> {launcher}" in lite.ok()
    lite.ok("--uninstall")
    assert not launcher.exists()


def _lite_json_under_hooks(lite: Lite) -> List[str]:
    return sorted(p.relative_to(lite.hooks).as_posix() for p in lite.hooks.rglob("*.json"))


def test_a_fresh_install_keeps_state_out_of_the_hooks_folder(lite: Lite) -> None:
    out = lite.ok()
    assert lite.state.is_dir() and not (lite.hooks / "state").exists()
    assert _lite_json_under_hooks(lite) == ["app-launcher-lite-chief-guard.json", "app-launcher-lite-session-state.json"]
    assert f"State file will appear at: {lite.state}" in out


def test_a_plain_install_leaves_old_state_where_it_is_and_says_to_migrate(lite: Lite) -> None:
    state = lite.seed_state()
    out = lite.ok()
    assert "[warn] Lite's state is still in hooks\\state" in out
    assert state.read_text(encoding="utf-8") == _STATE_BODY
    assert not lite.state.exists(), "an empty new folder would switch every writer away from the live state"


def _seed_full_state(lite: Lite) -> dict:
    """What a machine with a live chief holds: plan, journal, handover, registry, pending and claimed events."""
    legacy = lite.hooks / "state"
    files = {
        "sessions-state.json": '{"rows": []}',
        "chief-plan.json": '{"version": 1, "items": []}',
        "chief-managed.json": '{"sid-1": {"ref": "x#1"}}',
        "active-issues.json": "{}",
        "chief-journal.jsonl": '{"at": 1}\n{"at": 2}\n',
        "chief-handover.md": "# handover",
        "chief-inbox/1-turn-end-a.json": '{"kind": "turn-end"}',
        "chief-inbox/claimed/0-turn-end-b.json": '{"kind": "turn-end"}',
        "chief-inbox/.turn-end-sid-1.stamp": "",
    }
    for rel, body in files.items():
        (legacy / rel).parent.mkdir(parents=True, exist_ok=True)
        (legacy / rel).write_text(body, encoding="utf-8")
    return files


def test_migrate_moves_every_state_file_readable_and_a_second_run_changes_nothing(lite: Lite) -> None:
    files = _seed_full_state(lite)
    out = lite.ok("--migrate", "--yes")
    assert not (lite.hooks / "state").exists() and "[ok] old state folder removed" in out
    for rel, body in files.items():
        assert (lite.state / rel).read_text(encoding="utf-8") == body, rel
    assert _lite_json_under_hooks(lite) == ["app-launcher-lite-chief-guard.json", "app-launcher-lite-session-state.json"]

    before = _snapshot(lite.profile)
    again = lite.ok("--migrate", "--yes")
    assert "[ok] state already lives outside Copilot's hooks folder" in again
    assert _snapshot(lite.profile) == before


def test_migrate_points_settings_pinned_at_the_old_folder_at_the_new_one(lite: Lite) -> None:
    lite.seed_state()
    legacy = lite.hooks / "state"
    config = lite.write_config({"chief_inbox_dir": str(legacy / "chief-inbox"),
                                "chief_plan_file": str(lite.root / "elsewhere" / "plan.json"),
                                "gitlab_host": "git.example.test"})
    lite.ok("--migrate", "--yes")
    body = json.loads(config.read_text(encoding="utf-8"))
    assert body["chief_inbox_dir"] == str(lite.state / "chief-inbox")
    assert body["chief_plan_file"] == str(lite.root / "elsewhere" / "plan.json"), "a path of the owner's own stays"
    assert body["gitlab_host"] == "git.example.test"
    assert list((lite.repo / "config").glob("webapp_config.json.bak-*")), "a backup comes first"


def test_whatif_migrate_moves_no_state(lite: Lite) -> None:
    state = lite.seed_state()
    out = lite.ok("--migrate", "--yes", "--what-if")
    assert "[whatif] state copied, then switched ->" in out
    assert state.read_text(encoding="utf-8") == _STATE_BODY and not lite.state.exists()
