"""The gate's docs-, structure-, instruction-, doc-refs-, dead-code-, conventions-, runtime-write-, request-budget-, windows-only-, probe-fake-, port-host-, install-standard- and clean-tree-ratchet stages fail on exit 3 and on a new finding, by name.

The real stage text runs against a throwaway repo with `Fail` stubbed to exit 1; a bare
`& $python scripts/<ratchet>.py` would let exit 3 ("not confirmed") pass."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
GATE_PS1 = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = (pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the gate is a PowerShell script")


# A ratchet that refuses to run without its own keys (conventions needs `rules`); the
# throwaway repo has no [vendored] table, so runtime write is told to skip its own copy.
_EXTRA_KEYS = {
    "conventions": 'rules = ["logging-not-print"]\n',
    "runtime_write": 'exclude = ["scripts/**"]\n',
    "request_budget": 'browser_paths = ["static/**"]\n',
    "clean_tree": 'smoke = ["{python}", "smoke.py"]\n',
    "probe_fake": 'probes = ["src/a.py"]\nbase_ref = "HEAD"\n',
}
# Clean tree's baseline lists paths, not per-file counts.
_BASELINES = {"clean_tree": '{"version": 1, "paths": []}\n',
              "install_standard": '{"version": 1, "hits": []}\n',
              "probe_fake": '{"version": 1, "hits": []}\n'}
# A throwaway repo that meets the install standard, bar the four installer modules copied in below.
_INSTALLABLE = {
    "docs/install.md": "## Install\n\n## Update\n\n## Migrate\n",
    ".fleet.toml": "port = 8099\n[install.migrate]\ncopy = []\n[install.endpoint]\n[vendored]\n" + "".join(
        f"{key} = {{}}\n" for key in ("installer", "installer_migrate", "installer_declaration", "installer_inplace")),
}


def _stage(name: str) -> str:
    """The gate's real handler and stage table, then a call for the one named stage."""
    phase = name.replace("_", " ")
    text = GATE_PS1.read_text(encoding="utf-8")
    start = text.index("function Invoke-RatchetStage")
    end = text.index("# ratchet-stages:end")
    return (f"{text[start:end]}\n"
            f"Invoke-RatchetStage ($ratchetStages | Where-Object {{ $_.Name -eq '{phase}' }})")


@pytest.mark.parametrize("name, files, code, expected", [
    ("docs", {"README.md": "# T\n\n```\nnever closed\n"}, 1, ["docs ratchet NOT CONFIRMED ("]),
    ("docs", {"README.md": "# T\n\nSee #123 for why.\n"}, 1, ["docs ratchet failed", "README.md", "#123"]),
    ("docs", {"README.md": "# T\n\nPlain prose.\n"}, 0, ["STAGE-PASSED"]),
    ("structure", {"src/a.py": "x = 1\n", ".fleet.toml": 'shape = "nope"\n'}, 1,
     ["structure ratchet NOT CONFIRMED ("]),
    ("structure", {"src/big.py": "x = 1\n" * 1100}, 1, ["structure ratchet failed", "src/big.py: 1,100 lines"]),
    ("structure", {"src/a.py": "x = 1\n"}, 0, ["STAGE-PASSED"]),
    ("instruction", {"CLAUDE.md": "# T\n\n```\nnever closed\n"}, 1,
     ["instruction ratchet NOT CONFIRMED ("]),
    ("instruction", {"CLAUDE.md": "# T\n\nAlways double-check your work.\n"}, 1,
     ["instruction ratchet failed", "CLAUDE.md", "R-03"]),
    ("instruction", {"CLAUDE.md": "# T\n\nPlain prose.\n"}, 0, ["STAGE-PASSED"]),
    ("doc_refs", {"README.md": "# T\n\n```\nnever closed\n"}, 1, ["doc refs ratchet NOT CONFIRMED ("]),
    ("doc_refs", {"README.md": "# T\n\nRun `scripts/missing.py` first.\n"}, 1,
     ["doc refs ratchet failed", "README.md", "scripts/missing.py"]),
    ("doc_refs", {"README.md": "# T\n\nPlain prose.\n"}, 0, ["STAGE-PASSED"]),
    ("dead_code", {"src/a.py": "def broken(:\n"}, 1, ["dead code ratchet NOT CONFIRMED ("]),
    ("dead_code", {"src/a.py": "def never_called():\n    pass\n"}, 1,
     ["dead code ratchet failed", "src/a.py", "never_called"]),
    ("dead_code", {"src/a.py": "def used():\n    pass\n\n\nused()\n"}, 0, ["STAGE-PASSED"]),
    ("conventions", {"src/a.py": "def broken(:\n"}, 1, ["conventions ratchet NOT CONFIRMED ("]),
    ("conventions", {"src/a.py": "print('hi')\n"}, 1,
     ["conventions ratchet failed", "src/a.py", "[logging-not-print]"]),
    ("conventions", {"src/a.py": "import logging\n\nlogging.info('hi')\n"}, 0, ["STAGE-PASSED"]),
    ("runtime_write", {"src/a.py": "def broken(:\n"}, 1, ["runtime write ratchet NOT CONFIRMED ("]),
    ("runtime_write", {"src/a.py": "from pathlib import Path\n\nPath(__file__).with_name('out.txt').write_text('x')\n"}, 1,
     ["runtime write ratchet failed", "src/a.py"]),
    ("runtime_write", {"src/a.py": "x = 1\n"}, 0, ["STAGE-PASSED"]),
    ("request_budget", {"src/a.py": "def broken(:\n"}, 1, ["request budget ratchet NOT CONFIRMED ("]),
    ("request_budget", {"static/a.js": "setInterval(() => fetch('/api/x'), 1500);\n"}, 1,
     ["request budget ratchet failed", "static/a.js", "fixed-rate-poll"]),
    ("request_budget", {"static/a.js": "const x = 1;\n"}, 0, ["STAGE-PASSED"]),
    ("windows_only", {"tests/test_a.py": "def broken(:\n"}, 1, ["windows only ratchet NOT CONFIRMED ("]),
    ("windows_only", {"tests/test_a.py": "import winreg\n\n\ndef test_a():\n    pass\n"}, 1,
     ["windows only ratchet failed", "tests/test_a.py", "win-module"]),
    ("windows_only", {"tests/test_a.py": "def test_a():\n    pass\n"}, 0, ["STAGE-PASSED"]),
    ("probe_fake", {"tests/test_a.py": "def broken(:\n"}, 1, ["probe fake ratchet NOT CONFIRMED ("]),
    ("probe_fake", {"src/a.py": "x = 1\n"}, 1, ["probe fake ratchet failed", "src/a.py", "probe-fake-missing"]),
    ("probe_fake", {"src/a.py": "x = 1\n", "tests/test_a.py": "import src.a\n\nON = ['win32', 'darwin']\n"}, 0,
     ["STAGE-PASSED"]),
    ("port_host", {"src/a.py": "def broken(:\n"}, 1, ["port host ratchet NOT CONFIRMED ("]),
    ("port_host", {"src/a.py": "PORT = 8501\n"}, 1, ["port host ratchet failed", "src/a.py", "port-literal"]),
    ("port_host", {"src/a.py": "x = 1\n"}, 0, ["STAGE-PASSED"]),
    ("install_standard", {"src/installer_declaration.py": "", "src/installer_migrate.py": "def broken(:\n"}, 1,
     ["install standard ratchet NOT CONFIRMED ("]),
    ("install_standard", {}, 1, ["install standard ratchet failed", "installer-vendored", "src/installer.py"]),
    ("install_standard", _INSTALLABLE, 0, ["STAGE-PASSED"]),
    ("clean_tree", {"smoke.py": "raise SystemExit(1)\n"}, 1, ["clean tree ratchet NOT CONFIRMED ("]),
    ("clean_tree", {"smoke.py": "open('new.txt', 'w').write('x')\n"}, 1,
     ["clean tree ratchet failed", "new.txt", "tree-write"]),
    ("clean_tree", {"smoke.py": "pass\n"}, 0, ["STAGE-PASSED"]),
])
def test_stage_verdict(tmp_path, name: str, files: dict[str, str], code: int, expected: list[str]) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / f"{name}_ratchet.py", tmp_path / "scripts")
    # Every ratchet loads this sibling to find its baseline in .ratchets/.
    shutil.copy2(REPO_ROOT / "scripts" / "ratchet_baseline.py", tmp_path / "scripts")
    if name == "structure":
        for sibling in ("structure_layout.py", "structure_band.py"):
            shutil.copy2(REPO_ROOT / "scripts" / sibling, tmp_path / "scripts")
    if name == "instruction":
        for sibling in ("instruction_frontmatter.py", "instruction_repeats.py"):
            shutil.copy2(REPO_ROOT / "scripts" / sibling, tmp_path / "scripts")
    if name == "conventions":
        for sibling in ("conventions_readers.py", "conventions_state.py"):
            shutil.copy2(REPO_ROOT / "scripts" / sibling, tmp_path / "scripts")
    if name == "runtime_write":
        shutil.copy2(REPO_ROOT / "scripts" / "runtime_write_resolve.py", tmp_path / "scripts")
    if name == "request_budget":
        shutil.copy2(REPO_ROOT / "scripts" / "request_budget_scan.py", tmp_path / "scripts")
    if files is _INSTALLABLE:
        (tmp_path / "src").mkdir()
        for module in REPO_ROOT.glob("src/installer*.py"):
            shutil.copy2(module, tmp_path / "src")
    baseline = _BASELINES.get(name, '{"version": 1, "entries": []}\n')
    files = {f".{name.replace('_', '-')}-ratchet-baseline.json": baseline, **files}
    files[".fleet.toml"] = files.get(".fleet.toml", "") + f"[{name}_ratchet]\nenabled = true\n{_EXTRA_KEYS.get(name, '')}"
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(exist_ok=True)
        (tmp_path / path).write_text(text, encoding="utf-8")
    steps = [["init", "-q"], ["add", "."]]
    if name == "probe_fake":  # it diffs against a merge base, so the repo needs a commit
        # A throwaway repo: the machine's global hooks reject a test author.
        steps.append(["-c", f"core.hooksPath={tmp_path / 'no-hooks'}", "-c", "user.name=t",
                      "-c", "user.email=t@example.invalid", "commit", "-qm", "t"])
    for argv in steps:
        subprocess.run(["git", *argv], cwd=tmp_path, check=True, creationflags=_NO_WINDOW)
    harness = tmp_path / "stage.ps1"
    harness.write_text(
        "function Phase($m) {}\nfunction Fail($m) { Write-Host \"FAIL: $m\"; exit 1 }\n"
        f"$python = '{sys.executable}'\nSet-Location '{tmp_path}'\n"
        f"{_stage(name)}\nWrite-Host 'STAGE-PASSED'\n", encoding="utf-8")
    run = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=120)

    assert run.returncode == code, f"stdout={run.stdout!r} stderr={run.stderr!r}"
    for text in expected:
        assert text in run.stdout, run.stdout
    assert ("STAGE-PASSED" in run.stdout) == (code == 0), run.stdout
