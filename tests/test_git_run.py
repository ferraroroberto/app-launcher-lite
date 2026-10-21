"""``src/git_run.py`` — the one git invocation contract (issue #11).

The contract these assert is what the five former hand-typed copies had
drifted apart on: a non-zero git is a *result* the caller reads, "couldn't
run at all" is ``None``, and the console-suppressing / stdin-closing kwargs
are applied on every call rather than per site.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from src.build_info import resolve_git_sha
from src.git_run import DEFAULT_TIMEOUT_S, resolve_deployed_sha, run_git
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


def test_optional_locks_off_and_environment_inherited(monkeypatch, tmp_path):
    """#67: a read killed mid-``git status`` must not strand ``index.lock``.
    ``GIT_OPTIONAL_LOCKS=0`` is set on top of the inherited environment —
    replacing it would drop PATH / HOME / proxy settings git relies on."""
    seen = {}

    def _capture(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _capture)
    monkeypatch.setenv("LAUNCHER_PROBE_VAR", "kept")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")  # a parent value must lose
    run_git(tmp_path, ["status"])

    env = seen["env"]
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["LAUNCHER_PROBE_VAR"] == "kept"
    assert env.get("PATH") == os.environ.get("PATH")


def test_resolve_deployed_sha_returns_short_sha_for_this_repo(tmp_path):
    """#655: must not depend on the ambient CI checkout's ``origin`` remote
    state -- GitHub Actions' shallow PR checkout can leave both
    ``origin/HEAD`` and an ``origin/main`` tracking ref unresolvable, which
    is a property of that checkout, not a defect in this function. Exercise
    it against a throwaway repo we control instead (same fixture pattern as
    ``test_resolve_deployed_sha_differs_from_checkout_branch_tip`` below)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    no_hooks = tmp_path / "no-hooks"
    no_hooks.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-q", "-b", "main")
    run("config", "core.hooksPath", str(no_hooks))
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "base")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    run("remote", "add", "origin", str(remote))
    run("push", "-q", "origin", "main")
    run("remote", "set-head", "origin", "main")

    sha = resolve_deployed_sha(repo)
    assert isinstance(sha, str) and sha
    assert sha != "unknown"
    assert len(sha) <= 12


def test_resolve_deployed_sha_unknown_for_non_repo_dir(tmp_path):
    assert resolve_deployed_sha(tmp_path) == "unknown"


def test_resolve_deployed_sha_unknown_when_git_missing(monkeypatch, tmp_path):
    def _raise(*_args, **_kwargs):
        raise OSError("git not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert resolve_deployed_sha(tmp_path) == "unknown"


def test_resolve_deployed_sha_differs_from_checkout_branch_tip(tmp_path):
    """#641: on a feature branch that hasn't merged, the checkout's own
    branch tip (``resolve_git_sha``) and the resolved deployed ref
    (``resolve_deployed_sha``, ``origin/main``) must be free to diverge —
    the whole point of having two separate functions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    no_hooks = tmp_path / "no-hooks"
    no_hooks.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-q", "-b", "main")
    run("config", "core.hooksPath", str(no_hooks))
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "base")

    # A bare remote so origin/HEAD + origin/main resolve without a network.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    run("remote", "add", "origin", str(remote))
    run("push", "-q", "origin", "main")
    run("remote", "set-head", "origin", "main")

    main_sha = resolve_deployed_sha(repo)

    run("checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("v2", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "feature work, not pushed")
    branch_tip = resolve_git_sha(repo)
    deployed = resolve_deployed_sha(repo)

    assert branch_tip != main_sha
    assert deployed == main_sha
