"""Shared `git`/`gh` subprocess wrappers for skills/_lib helpers.

Every helper here needs the same three lines — shell out to `git`, capture
stdout/stderr as UTF-8, never crash on an undecodable byte (`errors="replace"`,
so a non-ASCII commit message cannot raise `UnicodeDecodeError` mid-run).
Hand-rolled copies in `audit_issue.py`, `cert_drift.py`, `dirty_tree_check.py`,
`ux_surface.py` and `worktree_claim.py` drift apart in error handling, so one
place owns the subprocess plumbing; each call site keeps whatever return shape (`str` vs
`CompletedProcess`) and failure behavior (raise vs swallow) it needs on top.

`run_gh` is the same fix for the `gh` CLI: `audit_issue.py`,
`chief_ops.py`, `issue_state_gate.py`, and `gh_issue_fetch.py` each hand-rolled
an identical `capture_output=True, text=True, encoding="utf-8",
errors="replace", creationflags=NO_WINDOW` `gh` spawn before this existed, and
had already drifted on `timeout` (some passed none, some 30s/60s/120s) — see
that function's docstring.

stdlib only (plus the sibling `no_window` constant).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from no_window import NO_WINDOW  # noqa: E402

# Bound for a `gh ... list --json` read in an unattended weekly run, shared by the
# scheduled skills that page a repo's PRs or issues. A stated guess, not a
# measured one: a 200-400 item page normally returns in seconds, and 120 s
# covers a slow GitHub API while still failing the call, rather than hanging
# the whole `claude -p` run, if it never answers.
GH_LIST_TIMEOUT_S = 120


def git_env(base: Optional[dict] = None) -> dict:
    """`base` (default `os.environ`) plus `GIT_OPTIONAL_LOCKS=0`.

    **Do not "clean up" this variable.** `git status` takes
    `.git/index.lock` purely to write back a refreshed stat
    cache — an optimisation, not part of producing the output. Kill such a
    process mid-refresh and it leaves exactly a 0-byte `index.lock` and
    nothing else touched. Done fleet-wide, that strands a 0-byte lock in
    every repo swept, all stamped the same second, and each is silently unable
    to `git add`/`commit`/`pull`/`stash` for days.

    Days, because a stale lock is invisible to a read: `git status
    --porcelain`, `git fetch`, `git rev-list`, and an up-to-date `git pull
    --ff-only` **all exit 0 and print the right answer** with the lock
    sitting there — so the raise-on-non-zero guard never fires and no sweep
    ever reports `UNKNOWN`. Detection is `index_lock.py`'s job; this is the
    half that stops them being created at all.

    Measured, and narrower than "and friends" suggested:
    of `status --porcelain=v2 --branch`, `diff --name-only HEAD`,
    `diff --name-only --cached HEAD`, `ls-files --others`,
    `rev-parse --verify` and `symbolic-ref`, **only `status` takes the
    optional lock**. A helper that never runs `status` cannot strand one —
    which lets a hunt for stranded locks rule call sites in and out instead
    of suspecting every `git` spawn equally.

    `GIT_OPTIONAL_LOCKS=0` suppresses only *optional* locks — a requested
    write still takes the real index lock, and still refuses when one is
    already held. Verified, not assumed: `tests/repo/test_git_run.py` drives both
    halves against a real repo.
    """
    env = dict(os.environ if base is None else base)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run_git(
    args: Sequence[str], *, check: bool = False, timeout: Optional[float] = None,
    extra_env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run `git <args>`, UTF-8 decoded with undecodable bytes replaced.

    Pass `-C <repo>` as part of `args` to target a specific working tree —
    the convention every call site in this repo uses. `check=True` raises
    `subprocess.CalledProcessError` on a non-zero exit (matching
    `subprocess.run`'s own contract); the default `check=False` leaves the
    caller to inspect `.returncode`/`.stdout`/`.stderr`. `timeout` is passed
    straight through (raising `subprocess.TimeoutExpired`) for the call sites
    that bound a crawl over an unknown tree.

    `creationflags=NO_WINDOW` is what makes this the *only* place most helpers
    need to think about console suppression: a scheduled `claude -p` job has no
    console of its own, so every unsuppressed `git` spawn beneath it flashes a
    window on screen. `env=git_env()` is the same
    one-place-fix argument for optional index locks — see that function.

    `stdin=DEVNULL` because git is never fed input and a hidden, detached
    parent leaves an invalid inherited handle (`OSError: [WinError 6]`).

    `extra_env` layers variables that have no `-c` equivalent (e.g.
    `GIT_INDEX_FILE`) over the ambient environment;
    `git_env()` is applied last, so no caller can drop `GIT_OPTIONAL_LOCKS=0`.
    """
    base = {**os.environ, **extra_env} if extra_env else None
    return subprocess.run(
        ["git", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=check, timeout=timeout,
        creationflags=NO_WINDOW, env=git_env(base), stdin=subprocess.DEVNULL,
    )


def run_gh(
    args: Sequence[str], *, check: bool = False, timeout: Optional[float] = None,
    stdin: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """Run `gh <args>`, UTF-8 decoded with undecodable bytes replaced.

    The `gh`-CLI sibling of `run_git`. `text=True` alone
    falls back to cp1252 on Windows, which raises `UnicodeDecodeError` on a
    non-ASCII issue title or body — every prior call site
    repeated that comment along with the identical spawn. `timeout` stays a
    parameter rather than a forced constant: call sites had already drifted
    (`issue_state_gate` 30s, `gh_issue_fetch` 60s, `audit_issue`/`chief_ops`
    none) before this wrapper existed to catch the drift, and that spread is
    real per-call-site policy (a short interactive-shaped lookup vs. a bulk
    `--limit 400` list), not an oversight to erase. `stdin` likewise stays
    optional — `None` inherits, matching every site except
    `chief_ops.fetch_issue_state`, which passes `subprocess.DEVNULL` to keep a
    scheduled run from ever blocking on stdin.

    `creationflags=NO_WINDOW` is the same one-place fix as `run_git`.
    """
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=check, timeout=timeout,
        creationflags=NO_WINDOW, stdin=stdin,
    )


def run_git_bytes(
    args: Sequence[str], *, check: bool = False, timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    """`run_git` for the callers that need **raw bytes**, not decoded text.

    Same spawn, same `creationflags=NO_WINDOW` and `env=git_env()` — only the
    decoding differs. Exists because `git show <ref>:<path>` and
    `git ls-tree` are read for byte-exact hashing (`vendored_drift`) or for
    content that must round-trip untouched (`system-map`/`config-map`
    `build_data`), and `run_git`'s `errors="replace"` would silently rewrite a
    byte before it was hashed. Those call sites used to hand-roll their own
    `subprocess.run(["git", …])` to get bytes, which also opted them out of
    `git_env()` — the wrapper is the *only* thing that had to be given up, so
    now it isn't.
    """
    return subprocess.run(
        ["git", *args], capture_output=True, check=check, timeout=timeout,
        creationflags=NO_WINDOW, env=git_env(), stdin=subprocess.DEVNULL,
    )


class Unreadable(Exception):
    """A repo's facts could not be established — so there is no verdict.

    Raised by `run_git_or_raise` instead of letting a failed `git` return an
    empty string that then reads as a fact: empty porcelain reads as *clean*
    and an empty branch name is unequal to `main`, so a repo that was never
    inspected would otherwise be reported as available, or dispatched, or
    called DIRTY — all of them inventions.

    One class for both `dirty_tree_check` and `repo_preflight`.
    They used to carry byte-identical private copies under
    a comment claiming a shared raiser could not satisfy both "different
    vocabularies" — untrue: the vocabularies differ in how each *caller*
    renders the caught exception, not in the raising, and both re-exported
    names still resolve here so `except dirty_tree_check.Unreadable` keeps
    working.
    """


def run_git_or_raise(repo_path: Path, *args: str) -> str:
    """Stripped stdout for `git -C <repo_path> <args>`, or `Unreadable`.

    Never an empty string standing in for a fact — see `Unreadable`. The
    message carries the exit code plus git's first stderr line, which is what
    makes an unreadable repo diagnosable from a sweep's output alone.
    """
    r = run_git(["-C", str(repo_path), *args])
    if r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()
        raise Unreadable(
            f"git {' '.join(args)} failed (exit {r.returncode})"
            + (f": {detail[0]}" if detail else "")
        )
    return r.stdout.strip()


def run_git_checked(args: Sequence[str]) -> str:
    """Run `git <args>`; raise `SystemExit` on a non-zero exit, else return
    stripped stdout.

    The convenience wrapper every call site actually wants on top of
    `run_git` — most fleet code doesn't want to inspect `.returncode` itself,
    it wants "give me the output or blow up with a clear message." Was
    `audit_issue.py`'s private `_git`, promoted here so
    `design_sweep_scan.py` / `fleet_audit_scan.py` reach a real public entry
    point instead of a sibling module's underscore-prefixed name.
    """
    r = run_git(args)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        raise SystemExit(f"git {' '.join(args)} failed (exit {r.returncode})")
    return (r.stdout or "").strip()


def changed_files(repo_path: Path, base: str) -> Optional[List[str]]:
    """Files changed on HEAD since its merge-base with `base` (three-dot), or
    `None` when the diff could not be taken at all.

    **`None`, never `[]`.** An unresolvable base ref, a detached/unborn HEAD,
    an unreadable repo — none of those mean "nothing was touched", but each of
    `deploy_coverage`, `ux_surface` and `docs_shots_plan` used to carry its own
    verbatim copy of this helper that swallowed the non-zero exit and returned
    an empty list, rendering as `TOUCHED=no` / `STALE=` / `UNMAPPED=`: a failed
    probe indistinguishable from a clean diff, in the very machinery those
    gates exist to make honest. `deploy_coverage`'s own
    module docstring already spelled out the rule its `_changed_files` broke —
    "a flow that cannot tell whether it was touched must not silently assume it
    wasn't". Callers turn `None` into their own
    `unknown` state; none of them may turn it back into `no`.
    """
    res = run_git(["-C", str(repo_path), "diff", "--name-only", f"{base}...HEAD"])
    if res.returncode != 0:
        return None
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


_GITHUB_SLUG_RE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def slug_from_url(url: Optional[str]) -> Optional[str]:
    """`owner/repo` parsed out of a GitHub remote URL (https or ssh), or `None`.

    The one regex now shared by `origin_slug` below and the callers that
    already have the URL in hand and only need the parse -- four
    near-identical copies of this same pattern before.
    """
    m = _GITHUB_SLUG_RE.search(str(url or "").strip())
    return m.group(1) if m else None


def origin_slug(repo_path: Path, *, timeout: Optional[float] = None) -> Optional[str]:
    """`owner/repo` from `repo_path`'s GitHub `origin` remote, or `None`.

    The `git remote get-url origin` + regex pair that six call sites across
    the repo used to hand-roll on their own, with four different regexes
    between them: `e2e_value/sources.py`,
    `design_review/filing.py`, `branch_residue.py`, `active_issue.py`,
    `chief/chief_sweep.py`, and `fleet_core/fleet_repo_scan.py`.
    """
    res = run_git(["-C", str(repo_path), "remote", "get-url", "origin"], timeout=timeout)
    return slug_from_url(res.stdout) if res.returncode == 0 else None


def resolve_default_branch_ref(
    repo_path: Path,
    candidates: Sequence[str] = ("origin/main", "main", "master"),
    final_fallback: str = "main",
) -> str:
    """The repo's default branch, preferring the remote's own `origin/HEAD`.

    On `symbolic-ref refs/remotes/origin/HEAD` success, returns the ref with
    the `refs/remotes/` prefix stripped (e.g. `origin/main`). On failure,
    probes `candidates` in order via `rev-parse --verify --quiet` and returns
    the first that resolves; if none do (or `candidates` is empty), returns
    `final_fallback`. `candidates`/`final_fallback` are parameterized so a
    caller with different fallback semantics can reproduce
    its own exact behavior on top of this one implementation.
    """
    res = run_git(["-C", str(repo_path), "symbolic-ref", "refs/remotes/origin/HEAD"])
    ref = res.stdout.strip()
    if res.returncode == 0 and ref:
        return ref.replace("refs/remotes/", "", 1)
    for cand in candidates:
        if run_git(["-C", str(repo_path), "rev-parse", "--verify", "--quiet", cand]).returncode == 0:
            return cand
    return final_fallback
