"""``scripts/sync_gitlab_mirror.py``: fast-forward only, and a diverged mirror is refused.

Real git against local bare repos standing in for GitHub (``origin``) and
GitLab (``gitlab``); no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import sync_gitlab_mirror as mirror
from src.jobs.outcome import OUTCOME_FAILED, run_outcome
from src.jobs.run_status import RunStatus, finished_status
from src.subprocess_flags import NO_WINDOW


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        creationflags=NO_WINDOW, timeout=30,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path):
    """A work clone whose ``origin`` and ``gitlab`` are two empty bare repos."""
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    bares = {}
    for name in ("origin", "gitlab"):
        bares[name] = tmp_path / f"{name}.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bares[name]))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    for key, value in (("core.hooksPath", str(hooks)), ("user.email", "t@example.com"),
                       ("user.name", "Test")):
        _git(work, "config", key, value)
    for name, path in bares.items():
        _git(work, "remote", "add", name, str(path))
    _commit(work, "base")
    _git(work, "push", "-q", "origin", "main")
    return work, bares


def _mirror_main(bares) -> str:
    return _git(bares["gitlab"], "rev-parse", "refs/heads/main")


def test_first_run_fills_an_empty_mirror_with_main_and_tags(world):
    work, bares = world
    _git(work, "tag", "v1")
    _git(work, "push", "-q", "origin", "--tags")
    report = mirror.sync(work)
    assert "pushed main and tags" in report
    assert _mirror_main(bares) == _git(work, "rev-parse", "HEAD")
    assert _git(bares["gitlab"], "tag") == "v1"


def test_a_level_mirror_is_left_alone(world):
    work, bares = world
    mirror.sync(work)
    assert "already at" in mirror.sync(work)


def test_a_mirror_that_is_behind_is_fast_forwarded(world):
    work, bares = world
    mirror.sync(work)
    head = _commit(work, "next")
    _git(work, "push", "-q", "origin", "main")
    mirror.sync(work)
    assert _mirror_main(bares) == head


def test_a_mirror_with_commits_origin_lacks_is_refused_and_untouched(world):
    work, bares = world
    mirror.sync(work)
    _git(work, "checkout", "-q", "-b", "side")
    stray = _commit(work, "only-on-gitlab")
    _git(work, "push", "-q", "gitlab", "side:main")
    _git(work, "checkout", "-q", "main")
    _commit(work, "only-on-github")
    _git(work, "push", "-q", "origin", "main")

    with pytest.raises(mirror._Outcome) as refused:
        mirror.sync(work)
    assert refused.value.code == mirror.EXIT_REFUSED
    assert "has commits" in str(refused.value)
    assert _mirror_main(bares) == stray


def test_main_reports_the_refusal_as_exit_one(world):
    work, bares = world
    mirror.sync(work)
    _git(work, "checkout", "-q", "-b", "side")
    _commit(work, "only-on-gitlab")
    _git(work, "push", "-q", "gitlab", "side:main")
    assert mirror.main(["--repo", str(work)]) == mirror.EXIT_REFUSED


@pytest.mark.parametrize("code", [mirror.EXIT_REFUSED, mirror.EXIT_UNKNOWN])
def test_the_jobs_tab_reads_a_nonzero_mirror_exit_as_failed(code):
    """The mirror is a plain Job: refused and not-established both end red, never success."""
    assert finished_status(code) is RunStatus.FAILED
    record = {"status": RunStatus.FAILED.value, "exit_code": code}
    assert run_outcome(record, adapter_run=False) == (OUTCOME_FAILED, None)


def test_an_unreachable_mirror_is_unknown_not_fine(world, tmp_path):
    work, _ = world
    _git(work, "remote", "set-url", "gitlab", str(tmp_path / "missing.git"))
    assert mirror.main(["--repo", str(work)]) == mirror.EXIT_UNKNOWN
