"""The repo PowerShell launcher under the AllSigned execution policy.

`powershell -File x.ps1` refuses an unsigned script under AllSigned, and
`-ExecutionPolicy Bypass` would weaken the machine's own control. The launcher
starts the script as a ScriptBlock instead. The AllSigned runs use the
`PSExecutionPolicyPreference` environment variable: it sets the *process*-scope
policy of that one PowerShell process tree and touches no machine or user setting.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.launching import ps_script
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[3]
POWERSHELL = ps_script.POWERSHELL_EXE

_PROBE = """\
param([Parameter(Position = 0)][string] $Action, [string] $Name, [string] $Empty, [switch] $Flag)
Write-Host "action=$Action name=[$Name] empty=[$Empty] flag=$($Flag.IsPresent) root=[$PSScriptRoot]"
exit 7
"""


def _launch(script: Path, args: list[str], policy: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PSExecutionPolicyPreference": policy, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "src.launching.ps_script", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=REPO_ROOT, env=env, creationflags=NO_WINDOW,
    )


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    path = tmp_path / "dir with space" / "probe.ps1"
    path.parent.mkdir()
    path.write_text(_PROBE, encoding="utf-8")
    return path


class TestScriptCommand:
    @pytest.mark.parametrize("policy", ["bypass", "unrestricted", "remotesigned"])
    def test_a_policy_that_runs_local_files_keeps_the_plain_file_launch(self, policy: str) -> None:
        argv, extra = ps_script.script_command("x.ps1", ["launch", "-Restart"], policy=policy)
        assert argv == [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", "x.ps1", "launch", "-Restart"]
        assert extra == {}

    @pytest.mark.parametrize("policy", ["allsigned", "restricted", "undefined", None])
    def test_any_other_policy_or_an_unread_one_loads_the_script_as_text(self, policy: object) -> None:
        argv, extra = ps_script.script_command("x.ps1", ["launch", "", "a b"], policy=policy)  # type: ignore[arg-type]
        assert argv[:3] == [POWERSHELL, "-NoProfile", "-NonInteractive"] and argv[3] == "-Command"
        assert "-File" not in argv
        assert extra[ps_script.ENV_SCRIPT] == "x.ps1"
        assert extra[ps_script.ENV_ARGV].split(chr(31))[1:] == ["launch", "", "a b"]

    def test_no_launch_ever_overrides_the_policy(self) -> None:
        for policy in ("allsigned", "bypass", None):
            argv, _ = ps_script.script_command("x.ps1", [], policy=policy)
            assert "-ExecutionPolicy" not in argv


@pytest.mark.windows_only
class TestUnderAllSigned:
    def test_the_control_a_plain_file_launch_is_refused(self, probe: Path) -> None:
        done = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "AllSigned", "-File", str(probe)],
            capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW,
        )
        assert done.returncode != 0 and "not digitally signed" in done.stderr

    def test_the_launcher_runs_it_with_args_root_and_exit_code(self, probe: Path) -> None:
        done = _launch(probe, ["launch", "-Name", "two words", "-Empty", "", "-Flag"], "AllSigned")
        assert done.returncode == 7, done.stderr
        assert f"action=launch name=[two words] empty=[] flag=True root=[{probe.parent}]" in done.stdout
        assert "execution policy allsigned" in done.stderr

    def test_a_relative_script_path_still_finds_its_folder(self, probe: Path) -> None:
        """The gate is started as `scripts/verify-before-ship.ps1` from the repo root."""
        relative = Path(os.path.relpath(probe, REPO_ROOT)) if probe.drive == REPO_ROOT.drive else probe
        done = _launch(relative, ["launch"], "AllSigned")
        assert done.returncode == 7, done.stderr
        assert f"root=[{probe.parent}]" in done.stdout

    def test_under_the_default_policy_the_file_launch_is_unchanged(self, probe: Path) -> None:
        done = _launch(probe, ["launch", "-Name", "x"], "Unrestricted")
        assert done.returncode == 7, done.stderr
        assert "action=launch name=[x] empty=[] flag=False" in done.stdout
        assert "with -File" in done.stderr

    def test_a_script_error_exits_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.ps1"
        bad.write_text("throw 'boom'\n", encoding="utf-8")
        done = _launch(bad, [], "AllSigned")
        assert done.returncode == 1 and "boom" in done.stderr

    def test_the_tray_helper_starts_under_allsigned(self, tmp_path: Path) -> None:
        """`tray.bat` / `tray.bat --restart` start this script; `detect` is its read-only action."""
        done = _launch(
            REPO_ROOT / "scripts" / "tray_lifecycle.ps1",
            ["detect", "-VenvDir", str(tmp_path), "-TrayMatch", "no-such-tray-xyz"], "AllSigned",
        )
        assert done.returncode == 0, done.stderr
        assert "cannot be loaded" not in done.stderr

    @pytest.mark.parametrize("policy", ["AllSigned", "Unrestricted"])
    def test_the_launcher_is_not_mistaken_for_the_tray_it_starts(self, policy: str) -> None:
        """Its command line carries the venv path and the tray's launch args, which `detect`
        matches on; reporting it as a tray makes `tray.bat` say "already running" and a
        `--restart` stop the launcher that is running it."""
        marker = f"psl-marker-{os.getpid()}"
        done = _launch(
            REPO_ROOT / "scripts" / "tray_lifecycle.ps1",
            ["detect", "-VenvDir", str(Path(sys.executable).parent.parent), "-TrayMatch", marker, "-TrayLaunch", marker],
            policy,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == ""

    def test_the_session_host_restart_starts_under_allsigned(self) -> None:
        """Bare, without -Confirm, it only prints its warning and exits 1, so this kills nothing."""
        done = _launch(REPO_ROOT / "scripts" / "restart-session-host.ps1", [], "AllSigned")
        assert done.returncode == 1, done.stderr
        assert "Re-run with -Confirm" in done.stdout
        assert "cannot be loaded" not in done.stderr

    def test_the_gate_sibling_scripts_load_as_text_under_allsigned(self, tmp_path: Path) -> None:
        """The two dot-sources verify-before-ship.ps1 runs, as the gate writes them."""
        gate_text = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
        loads = re.findall(r"^\s*\. \(\[scriptblock\]::Create.*$", gate_text, re.MULTILINE)
        assert len(loads) == 2, loads
        harness = tmp_path / "harness.ps1"
        harness.write_text(
            "\n".join(re.sub(r"; Enter-BoxLockGate$", "", line).strip() for line in loads)
            + "\nGet-Command Enter-BoxLockGate, Get-UnitWorkerArgs | Out-Null\nWrite-Host loaded\n",
            encoding="utf-8",
        )
        scripts = REPO_ROOT / "scripts"
        text = harness.read_text(encoding="utf-8").replace("$PSScriptRoot", f"'{scripts}'")
        harness.write_text(text, encoding="utf-8")
        done = _launch(harness, [], "AllSigned")
        assert done.returncode == 0 and "loaded" in done.stdout, done.stderr


class TestNoPolicyOverride:
    """Acceptance: no repo script starts PowerShell with `-ExecutionPolicy Bypass`."""

    def test_no_launch_site_passes_it(self) -> None:
        sites = [REPO_ROOT / "tray.bat", REPO_ROOT / "app" / "tray" / "tray.py",
                 *(REPO_ROOT / "scripts").glob("*.ps1"), *(REPO_ROOT / "src" / "launching").glob("ps_script.py")]
        offenders = [str(p.relative_to(REPO_ROOT)) for p in sites
                     if re.search(r"-ExecutionPolicy\s+Bypass|\"-ExecutionPolicy\"", p.read_text(encoding="utf-8"), re.I)]
        assert offenders == []

    def test_the_gate_never_dot_sources_a_script_file(self) -> None:
        gate = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
        assert not re.search(r"^\s*\. \(Join-Path", gate, re.MULTILINE)


class TestPlainPullOnAnAllSignedMachine:
    """A signature appended to a tracked script, or a documented `-File` start that would need
    one, turns every `git pull` on an AllSigned machine into a conflict."""

    def test_no_repo_script_carries_a_signature_block(self) -> None:
        scripts = [*REPO_ROOT.glob("*.ps1"), *(REPO_ROOT / "scripts").glob("*.ps1")]
        signed = [p.name for p in scripts if "SIG # Begin signature block" in p.read_text(encoding="utf-8")]
        assert scripts and signed == []

    def test_the_session_host_restart_is_documented_through_the_launcher(self) -> None:
        docs = [REPO_ROOT / "README.md", REPO_ROOT / "CLAUDE.md", *(REPO_ROOT / "docs").glob("*.md"),
                REPO_ROOT / "scripts" / "restart-session-host.ps1"]
        offenders = [p.name for p in docs if "-File scripts/restart-session-host.ps1" in p.read_text(encoding="utf-8")]
        assert offenders == []


class TestEffectivePolicy:
    def test_a_probe_that_cannot_run_is_not_confirmed_never_assumed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: object, **_k: object) -> object:
            raise OSError("no powershell")

        monkeypatch.setattr(ps_script.subprocess, "run", boom)
        assert ps_script.effective_policy() is None

    @pytest.mark.windows_only
    def test_it_reads_the_process_scope_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PSExecutionPolicyPreference", "AllSigned")
        assert ps_script.effective_policy() == "allsigned"

    @pytest.mark.windows_only
    def test_a_powershell_7_module_path_does_not_break_the_probe(self) -> None:
        """A pwsh 7 caller (an agent shell) passes its PSModulePath down to the 5.1 probe."""
        if "powershell_7" not in os.environ.get("PSModulePath", "").lower():
            pytest.skip("the test runner is not under a PowerShell 7 module path")
        assert ps_script.effective_policy() is not None
