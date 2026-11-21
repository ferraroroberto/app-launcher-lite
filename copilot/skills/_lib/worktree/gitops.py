"""Git and junction operations behind `worktree_claim.py`: create and tear down worktrees.

Thin subprocess wrappers plus the setup/teardown sequences whose ordering is
load-bearing (a junction is always stripped before any recursive delete).
`worktree_claim.py` re-exports these names for its CLI and its callers.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import git_run
from fleet_core import fleet_toml, runtime_files
from no_window import NO_WINDOW
from worktree_residue import WT_SEP

from worktree.claim import LOCK_NAME, worktree_path
from worktree.worktree_config import (
    copy_env_file,
    copy_root_config,
    copy_runtime_config,
    release_ports,
)

# The CLI a stale-worktree hint tells the reader to run.
_CLI = Path(__file__).resolve().parent.parent / "worktree_claim.py"

# `mklink /J` and `rmdir` on a junction return in milliseconds; 30 s bounds a wedged
# console host without cutting a slow disk short.
JUNCTION_TIMEOUT_S = 30

# ---- git / junction ops (Windows; thin subprocess wrappers) ---------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return git_run.run_git(["-C", str(repo), *args], check=check)


def common_dir(repo: Path) -> Path:
    """The shared git dir (one per repo, visible from every worktree)."""
    out = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(out)


def lock_dir_for(repo: Path) -> Path:
    return common_dir(repo) / LOCK_NAME


def main_ref(repo: Path) -> str:
    """origin's default branch, e.g. 'origin/main'. Falls back to origin/main.

    Routed through the shared `git_run.resolve_default_branch_ref` resolver;
    `candidates=()` + `final_fallback="origin/main"`
    reproduce this helper's own pre-existing symbolic-ref-only contract.
    """
    return git_run.resolve_default_branch_ref(repo, candidates=(), final_fallback="origin/main")


def count_behind(repo: Path, ref: str, *, fetch: bool = True) -> Optional[int]:
    """How many commits `repo`'s HEAD is behind `ref`, or None if uncountable.

    `fetch=True` refreshes the remote ref first, because the pre-landing count
    decides whether the service guard is skipped: a stale
    `origin/<default>` would read a tree that is genuinely behind as level, and
    a false `PRIMARY=live behind=0` is the one error direction this guard
    cannot afford. `land-primary` runs right after a `gh pr merge` that never
    touched local refs, so the stale case is the *normal* one, not the corner.
    Pass `fetch=False` when the ref was already fetched.

    A failed fetch, a failed `rev-list`, or output that isn't a number returns
    `None` -- not 0. An uncountable distance is unknown, and unknown is never
    folded into the passing state.
    """
    if fetch:
        remote = ref.split("/", 1)[0] if "/" in ref else "origin"
        if _git(repo, "fetch", "--quiet", remote, check=False).returncode != 0:
            return None
    counted = _git(repo, "rev-list", "--count", f"HEAD..{ref}", check=False)
    out = counted.stdout.strip()
    return int(out) if counted.returncode == 0 and out.isdigit() else None


def branch_exists_on_remote(repo: Path, branch: str) -> bool:
    """True if `branch` still exists on origin (or as a local ref).

    Used to detect a leaked claim whose branch is already merged-and-deleted.
    Shells to `git ls-remote` — stdlib + git CLI only, per
    the module contract; no new dependency. On any git failure (offline, no
    remote, transient error) we return True: an inability to *prove* the branch
    is gone must never reclaim a possibly-live claim out from under a peer.
    """
    if not branch:
        return True
    res = _git(repo, "ls-remote", "--heads", "origin", branch, check=False)
    if res.returncode != 0:
        return True  # can't tell -> assume present, fall back to the TTL
    if res.stdout.strip():
        return True
    # Not on the remote — it may be a local-only branch not yet pushed.
    local = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return local.returncode == 0


def local_branch_exists(repo: Path, branch: str) -> bool:
    """True if `branch` exists as a local ref in this repo."""
    if not branch:
        return False
    res = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return res.returncode == 0


def remote_branch_exists(repo: Path, branch: str) -> bool:
    """True if `origin/<branch>` exists as a remote-tracking ref.

    Deliberately the *local* remote-tracking ref, not `ls-remote`: the ref is
    what `git worktree add -b <branch> origin/<branch>` will actually resolve,
    so probing anything else could promise a start point git then rejects.
    Unlike `branch_exists_on_remote` (which fails safe toward "present" to
    protect a peer's claim), an unprovable ref here must read False — the
    caller's fallback is to branch off main, which is always valid.
    """
    if not branch:
        return False
    res = _git(repo, "rev-parse", "--verify", "--quiet",
               f"refs/remotes/origin/{branch}", check=False)
    return res.returncode == 0


def branch_checked_out_at(repo: Path, branch: str) -> Optional[str]:
    """Path of the worktree that already has `branch` checked out, else None.

    Git allows one checkout per branch across a repo's worktrees, so this is
    the one `setup-worktree` failure that no start-point choice can fix — it
    earns a message naming the holding tree rather than a generic git dump.
    """
    res = _git(repo, "worktree", "list", "--porcelain", check=False)
    if res.returncode != 0:
        return None
    current: Optional[str] = None
    for line in (res.stdout or "").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.startswith("branch ") and current:
            if line[len("branch "):].strip() == f"refs/heads/{branch}":
                return current
    return None


def worktree_add_args(
    wt: Path, branch: str, local_exists: bool, remote_exists: bool, main_ref_value: str,
) -> List[str]:
    """The `git worktree add` argv tail for `branch`, by what already exists.

    Pure decision half of `setup_worktree`. `-b` means
    *create*, so the original unconditional `-b <branch> <main>` had two
    failure modes on a branch that already existed: a raw
    `CalledProcessError` traceback when the branch was local, and — worse,
    silently — a lane started at `main` when the branch existed only on
    `origin`, discarding the work already pushed to it. Three cases:

    - already local -> check it out, no start point (git rejects `-b` here)
    - on origin only -> create it from `origin/<branch>`, resuming that work
    - neither -> create it off the default branch, exactly as before
    """
    if local_exists:
        return ["worktree", "add", str(wt), branch]
    if remote_exists:
        return ["worktree", "add", str(wt), "-b", branch, f"origin/{branch}"]
    return ["worktree", "add", str(wt), "-b", branch, main_ref_value]


def is_primary_checkout(repo: Path) -> bool:
    """True for the primary checkout, False for a linked worktree."""
    git_dir = _git(repo, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    return Path(git_dir).resolve() == common_dir(repo).resolve()


def _common_dir_or_none(path: Path) -> Optional[Path]:
    """`common_dir(path)`, or None when `path` isn't inside a git checkout.

    The shared git dir is the repo's identity: it is the *same* absolute path
    seen from the primary and from every linked worktree, which is what lets
    `mode_check` decide "same repo?" without deciding "same tree?".
    """
    try:
        return common_dir(path).resolve()
    except (subprocess.CalledProcessError, OSError):
        return None


def mode_check(cwd_common: Optional[Path], arg_common: Optional[Path]) -> Tuple[bool, str]:
    """Can `mode` answer about the cwd checkout? Returns `(ok, reason)`.

    Pure reconciliation half of `cmd_mode`. Both arguments
    are shared-git-dir paths, so the comparison is at **repo** granularity, not
    tree granularity: a bare repo name resolved to the primary while the caller
    stands in `<repo>-wt-<N>` is a *match*, because the argument only says which
    repo the cwd is expected to belong to. Naming a genuinely different repo —
    or standing outside git entirely — is the caller error worth surfacing.
    """
    if cwd_common is None:
        return False, "cwd is not inside a git checkout"
    if arg_common is None:
        return False, "repo argument is not inside a git checkout"
    if cwd_common.resolve() != arg_common.resolve():
        return False, (f"repo argument names a different repo than the cwd "
                       f"({arg_common} vs {cwd_common})")
    return True, "repo argument and cwd agree on the repo"


def _is_primary_checkout_safe(path: Path) -> bool:
    """`is_primary_checkout`, tolerant of `path` not being a git checkout at all.

    `remove_worktree`'s guard must never itself explode on
    an unexpected target -- a path that isn't a git repo can't be a primary
    checkout either, so this returns False rather than raising, leaving the
    naming-convention guard to catch that case instead.
    """
    try:
        return is_primary_checkout(path)
    except subprocess.CalledProcessError:
        return False


def _looks_like_worktree_name(name: str) -> bool:
    """True if `name` matches the `<repo>{WT_SEP}<N>` naming convention.

    Belt-and-braces companion to the primary-checkout guard:
    a `remove-worktree` target whose basename doesn't look like a worktree at
    all is refused too, even if it somehow isn't detected as a primary
    checkout (e.g. not a git repo). Mirrors `primary_for_worktree`'s own
    `rpartition(WT_SEP)` inversion -- `WT_SEP` stays the one spelling of the
    separator.
    """
    stem, sep, issue = name.rpartition(WT_SEP)
    return bool(sep) and bool(stem) and bool(issue)


# `.fleet.toml` is repo-authored data that travels between repos, and a declared
# target is used to build an OS-level command. Validate it here, at the one
# place it enters, rather than trusting it at each use site. An allowlist rather
# than a denylist: every real target is an ordinary directory name (`.venv`,
# `vendor/comfyui`, `models/cache`), so nothing legitimate is turned away.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9 ._-]+$")


def _safe_relpath(rel: str) -> bool:
    """True if every component of `rel` is an ordinary directory name.

    Also rejects a drive-qualified entry (`C:/x` -> first part `C:\\`), which
    the `..` check alone would let through.
    """
    return all(_SAFE_COMPONENT.match(part) for part in Path(rel).parts)


def worktree_junction_targets(repo: Path) -> list:
    """Relative paths to junction from the primary checkout into a fresh worktree.

    Always starts with `.venv`. A repo can declare extra gitignored paths its
    own gate needs (a vendored install, a model cache, ...) via `.fleet.toml`'s
    optional `[worktree]` table:

        [worktree]
        extra_junctions = ["vendor/comfyui"]

    No `.fleet.toml`, no `[worktree]` table, or a malformed value all degrade
    to the `.venv`-only default -- an undeclared repo behaves exactly as
    before. Entries that aren't non-empty strings, that
    contain a `..` component, or whose components are not ordinary directory
    names (`_safe_relpath`) are silently dropped rather than junctioning
    outside the repo or being used unvalidated. Pure path/text logic --
    no filesystem checks here;
    `setup_worktree` skips a declared target that doesn't actually exist in
    the primary.
    """
    targets = [".venv"]
    # An unreadable .fleet.toml (a partially-deleted/leftover worktree: permission
    # error, vanished mid-read, ...) reads as absent: remove_worktree must still
    # strip .venv rather than crash before it gets the chance to.
    table = fleet_toml.table(fleet_toml.load(repo), "worktree")
    if table is None:
        return targets
    extra = table.get("extra_junctions")
    if not isinstance(extra, list):
        return targets
    for item in extra:
        if not isinstance(item, str) or not item.strip():
            continue
        rel = item.strip().strip("/\\")
        if rel and ".." not in Path(rel).parts and _safe_relpath(rel):
            targets.append(rel)
    return targets


def _junction(link: Path, target: Path) -> Tuple[bool, str]:
    """Create a directory junction `link` -> `target` (`mklink /J`).

    Creates `link`'s parent first -- `git worktree add` only populates tracked
    files, so a declared extra target's parent dir (e.g. `vendor/` for
    `vendor/comfyui`) may not exist yet. Returns `(ok, message)`; message is
    the combined stderr/stdout on failure, empty on success. Pure OS wrapper --
    the caller decides whether a failure is fatal.
    """
    runtime_files.make_dirs(link.parent, parents=True, exist_ok=True)
    # encoding pinned to the OEM code page: `mklink` is a native console tool
    # and emits cp850 here, which is not valid UTF-8. Under bare text=True the
    # decode yields empty output, so setup_worktree aborted with
    # "Failed to junction <rel> into the worktree:" and an *empty* reason
    # Same rule listening_ports() below already documents.
    try:
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, encoding="oem", errors="replace",
            creationflags=NO_WINDOW, timeout=JUNCTION_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"mklink /J did not finish within {JUNCTION_TIMEOUT_S}s"
    ok = res.returncode == 0 and link.exists()
    return ok, "" if ok else (res.stderr.strip() or res.stdout.strip())


def _strip_junction(path: Path) -> None:
    """Remove a directory junction by its reparse point ONLY, never its target.

    `rmdir` without `/s` removes an empty dir or a reparse point and refuses a
    non-empty real dir — so it can NEVER recurse into a junction's target. This
    is the load-bearing step: doing it before `git worktree remove` is what
    keeps the primary's real .venv intact.
    """
    if path.exists() or path.is_symlink():
        # encoding="oem" for the same reason as _junction: cmd's own error text
        # is OEM-encoded and bare text=True silently blanks it.
        try:
            subprocess.run(
                ["cmd", "/c", "rmdir", str(path)],
                capture_output=True, encoding="oem", errors="replace",
                creationflags=NO_WINDOW, timeout=JUNCTION_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            # Never carry on: an unconfirmed strip lets the recursive delete that
            # follows walk through the junction into its real target.
            sys.exit(f"rmdir of the junction {path} did not finish within {JUNCTION_TIMEOUT_S}s; "
                     f"stopping before any recursive delete can follow it")


def setup_worktree(repo: Path, issue: str, branch: str) -> Path:
    wt = worktree_path(repo, issue)
    if wt.exists():
        # NEVER recommend `git worktree remove` here. A stale worktree still
        # holds its `.venv` junction, and git's recursive delete follows it
        # into the primary's real venv -- which is exactly how this message
        # destroyed task-os's venv under a live app: the
        # lane did what the helper told it to. `remove-worktree` strips the
        # junction with a reparse-safe `rmdir` first.
        # Forward slashes throughout: this line exists to be pasted, and Git
        # Bash strips backslashes out of an unquoted Windows path (it is also
        # what `bash_windows_path_guard` refuses). A hint that trips another
        # guard is a hint nobody follows.
        _fwd = lambda p: str(p).replace("\\", "/")  # noqa: E731
        sys.exit(f"Worktree path already exists: {wt}\n"
                 f"Probably stale — clean with the reparse-safe teardown:\n"
                 f"  {_fwd(sys.executable)} {_fwd(_CLI)} "
                 f"remove-worktree {_fwd(wt)}\n"
                 f"Do NOT `git worktree remove` it: {_fwd(wt / '.venv')} is a junction "
                 f"to {_fwd(repo / '.venv')}, and git's recursive delete follows it and "
                 f"guts the primary's real venv (fleet-config#847).")
    add_args = worktree_add_args(
        wt, branch,
        local_exists=local_branch_exists(repo, branch),
        remote_exists=remote_branch_exists(repo, branch),
        main_ref_value=main_ref(repo),
    )
    res = _git(repo, *add_args, check=False)
    if res.returncode != 0:
        holder = branch_checked_out_at(repo, branch)
        if holder:
            sys.exit(f"Branch '{branch}' is already checked out at: {holder}\n"
                     f"One checkout per branch — finish or remove that worktree first.")
        sys.exit(f"Could not create the worktree for '{branch}' at {wt}\n"
                 f"git worktree add exited {res.returncode}: "
                 f"{(res.stderr or res.stdout or '').strip()}")
    assigned_ports: set = set()
    copied = copy_runtime_config(repo, wt, assigned_ports)
    if copied:
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"CONFIG_COPIED={len(copied)}: "
              f"{', '.join(p.name for p in copied)}", file=sys.stderr)

    env_copied = copy_env_file(repo, wt)
    if env_copied:
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print("ENV_COPIED=1: .env", file=sys.stderr)

    root_copied = copy_root_config(repo, wt, assigned_ports)
    if root_copied:
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"ROOT_CONFIG_COPIED={len(root_copied)}: "
              f"{', '.join(p.name for p in root_copied)}", file=sys.stderr)

    linked: list = []
    for rel in worktree_junction_targets(repo):
        src = repo / rel
        if not src.is_dir():
            # Declared-but-absent must not break setup:
            # .venv itself is optional the same way today.
            continue
        link = wt / rel
        ok, err = _junction(link, src)
        if not ok:
            # Roll back the half-made worktree so we never leave a broken one
            # behind. Strip every junction made so far FIRST (reparse-safe,
            # including this failed/partial one) so the rollback remove can't
            # follow any of them into the primary's real target.
            for done in linked:
                _strip_junction(done)
            _strip_junction(link)
            _git(repo, "worktree", "remove", "--force", str(wt), check=False)
            sys.exit(f"Failed to junction {rel} into the worktree: {err}")
        linked.append(link)
    return wt


def primary_for_worktree(wt: Path) -> Optional[Path]:
    """The primary checkout owning `wt`, or None if it can't be determined.

    Normally `git rev-parse --git-common-dir` from inside the worktree. That
    fails once the worktree has been **deregistered** but its directory still
    exists (its `.git` file is gone, so git exits 128) -- precisely the leftover
    a teardown is called to clean, and the state that used to crash
    `remove_worktree` with an unhandled CalledProcessError.
    So fall back to inverting `worktree_path`'s own `<repo>-wt-<N>` convention;
    that stays a single source of truth for the naming rather than a second
    hand-rolled guess. Returns None when neither route resolves, leaving the
    caller to degrade honestly rather than pretend.
    """
    try:
        return common_dir(wt).parent  # common dir is <primary>/.git
    except (subprocess.CalledProcessError, OSError):
        pass
    stem, sep, _issue = wt.name.rpartition(WT_SEP)
    if sep and stem:
        candidate = wt.parent / stem
        if (candidate / ".git").exists():
            return candidate
    return None


def remove_worktree(wt: Path, *, force_nonstandard_name: bool = False) -> int:
    """Reparse-safe teardown. Returns 0 on success, 1 if the tree survived.

    Refuses (returns 2, deletes nothing) before touching anything if `wt`
    isn't actually a linked worktree: a primary checkout
    (`--git-common-dir` == `--git-dir`) is always rejected, and a basename
    that doesn't match the `<repo>{WT_SEP}<N>` convention is rejected too
    unless `force_nonstandard_name` is set. This is what stops a caller that
    confuses this function's `wt` arg with every sibling subcommand's
    `repo_root` arg from `rmtree`-ing a repo's main working tree.

    Never raises on a leftover it was asked to clean: a directory that outlives
    its registration (a live process holding a file open inside it is the usual
    cause -- a leaked browser helper or backend server) must produce an honest
    non-zero exit naming the path, so the caller reports residue instead of a
    false clean.
    """
    if not wt.exists():
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"Worktree already gone: {wt}")
        return 0

    if _is_primary_checkout_safe(wt):
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"REFUSING: {wt} is a primary checkout, not a linked worktree -- "
              f"deleting it would destroy the repo's main working tree "
              f"(fleet-config#589). Nothing was touched.", file=sys.stderr)
        return 2

    if not force_nonstandard_name and not _looks_like_worktree_name(wt.name):
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"REFUSING: {wt.name!r} doesn't match the '<repo>{WT_SEP}<N>' worktree "
              f"naming convention -- refusing to delete a path that doesn't look like "
              f"a worktree. Pass --force-nonstandard-name to override "
              f"(fleet-config#589). Nothing was touched.", file=sys.stderr)
        return 2

    # MUST precede any recursive delete, on EVERY path through this function
    # (see _strip_junction): git's remove follows a junction into its real
    # target and destroys it. `.fleet.toml` is a tracked file, so it was
    # already checked out into `wt` by `git worktree add` -- reading targets
    # from `wt` itself (not the primary) needs no git and degrades safely if
    # the worktree is already deregistered.
    for rel in worktree_junction_targets(wt):
        _strip_junction(wt / rel)

    primary = primary_for_worktree(wt)
    if primary is not None:
        _git(primary, "worktree", "remove", "--force", str(wt), check=False)
        _git(primary, "worktree", "prune", check=False)
    else:
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"# could not resolve the primary for {wt}; "
              f"stripped the junction and falling back to a plain delete",
              file=sys.stderr)

    if wt.exists():
        # git declined (or was never reachable) and the tree is still here.
        # The junction is already gone, so this delete cannot escape the tree.
        shutil.rmtree(wt, ignore_errors=True)

    return _report_removal(wt)


def _report_removal(wt: Path) -> int:
    """1 when the tree survived, naming its likely holder; else release its ports, 0.

    **When the removal fails as "busy", suspect a leaked browser helper**:
    a Playwright/WebKit helper left by an e2e run
    inherits pytest's cwd, so a run inside `<repo>-wt-<N>` leaves helpers whose
    **current working directory** is the worktree, and Windows will not delete
    a live process's cwd. The holder may be genuinely **running**, or wedged mid-exit:
    `ExitStatus` set so `GetExitCodeProcess`/`taskkill`
    report it gone, but `GetProcessTimes` shows no exit time, Toolhelp32 shows
    a live thread, and its cwd handle is still held. Windows keeps an
    already-exited process's object (and its `tasklist`/WMI row) alive only
    while *some* handle to it remains -- that alone does not rule out this
    wedged state, so "the sweep calls it exited" is not proof the worktree is
    safe to retry.

    Diagnosing and clearing that is the e2e harness's job, not this module's.
    Run the adopting repo's own standalone sweep through its venv interpreter,
    then retry the teardown:

        <repo>/.venv/Scripts/python.exe tests/e2e/_browser_sweep.py <worktree-path> [--dry-run]

    Pointer only -- the sweep reads each process's cwd from its PEB (Win32 has
    no accessor for it) and lives in `project-scaffolding`'s e2e harness, which
    reports a wedged holder as `wedged:pins-scope` and exits non-zero when the
    path is pinned. Never reimplement it here.

    **A `wedged:pins-scope` verdict means retrying the teardown will not
    help** -- nothing can reap a wedged process. The directory is freed by
    closing the holder's cwd handle remotely (`RTL_USER_PROCESS_PARAMETERS
    .CurrentDirectory.Handle` from its PEB, then `NtDuplicateObject(...,
    DUPLICATE_CLOSE_SOURCE)`), not by killing or rebooting -- rebooting the
    host was tried and the directories came straight
    back. Working implementation: `home-automation`'s `scripts/unpin_directory.py`.
    """
    if wt.exists():
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"Worktree NOT removed (a live process is likely holding a file "
              f"inside it): {wt}", file=sys.stderr)
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"# most likely a leaked Playwright/WebKit helper from an e2e run, "
              f"holding the worktree as its cwd (project-scaffolding#203). "
              f"Clear it with that repo's own sweep, then retry:\n"
              f"#   <repo>/.venv/Scripts/python.exe tests/e2e/_browser_sweep.py "
              f"{wt} --dry-run", file=sys.stderr)
        return 1
    # Only once the tree is genuinely gone: a surviving worktree may still boot
    # and bind, so releasing its ports earlier invites the collision the
    # reservation prevents. `_live_rows` would prune them later; this frees them now.
    released = release_ports(wt)
    if released:
        # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
        print(f"PORTS_RELEASED={len(released)}: "
              f"{', '.join(str(p) for p in released)}", file=sys.stderr)
    # logging-not-print-exempt: KEY=value or status line the worktree_claim CLI's caller reads from this stream
    print(f"Removed worktree: {wt}")
    return 0
