"""``src/git_run.py`` — the one git invocation contract (issue #11).

The contract these assert is what the five former hand-typed copies had
drifted apart on: a non-zero git is a *result* the caller reads, "couldn't
run at all" is ``None``, and the console-suppressing / stdin-closing kwargs
are applied on every call rather than per site.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from src.git_run import DEFAULT_TIMEOUT_S, run_git
from src.subprocess_flags import NO_WINDOW


def test_returns_completed_process_for_a_successful_command(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    result = run_git(tmp_path, ["--version"])
    assert result is not None
    assert result.returncode == 0
    assert "git version" in result.stdout


def test_nonzero_exit_comes_back_as_a_result_not_none(tmp_path):
    """A non-repo is an *answer* — the caller decides what it means. Folding
    it into ``None`` is what stopped call sites from telling "git says no"
    apart from "git never ran"."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    result = run_git(tmp_path, ["rev-parse", "--short", "HEAD"])
    assert result is not None
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_none_when_git_cannot_run(monkeypatch, tmp_path):
    def _raise(*_args, **_kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert run_git(tmp_path, ["rev-parse", "HEAD"]) is None


def test_none_on_timeout(monkeypatch, tmp_path):
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert run_git(tmp_path, ["status"]) is None


def test_invocation_contract_is_applied_on_every_call(monkeypatch, tmp_path):
    """The whole point of the module: one place decides these kwargs."""
    seen = {}

    def _capture(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _capture)
    run_git(tmp_path, ["diff", "--name-only"])

    assert seen["cmd"] == ["git", "-C", str(tmp_path), "diff", "--name-only"]
    kwargs = seen["kwargs"]
    assert kwargs["capture_output"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == DEFAULT_TIMEOUT_S
    # #688's fleet convention — a console-less pythonw tray must not flash a
    # window per git call.
    assert kwargs["creationflags"] == NO_WINDOW


def test_timeout_is_overridable_per_call_site(monkeypatch, tmp_path):
    """``src.scanner`` allows itself more headroom than the default; nothing
    else about the contract changes with it."""
    seen = {}

    def _capture(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _capture)
    run_git(tmp_path, ["status"], timeout=10.0)
    assert seen["timeout"] == 10.0
    assert seen["creationflags"] == NO_WINDOW
