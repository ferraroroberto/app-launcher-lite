"""Concurrency primitive for the issue-* skills: claim-or-worktree.

Two top-level agent sessions working the **same** repo used to collide because
they shared one working directory — a `git checkout` in session B rewrote the
tree under session A mid-build. The collision window is not
branch-cut time; it is the minutes-long *study* phase before an agent writes
anything. By the time the second agent cuts its branch, the damage is done.

The fix is first-come-first-served, claimed at the **very first action**:

  - The first session to `acquire` a repo wins its **primary checkout** and works
    on `main` in place, exactly as before.
  - Every session after that gets `MODE=worktree` and builds in an isolated
    sibling worktree (`<repo>-wt-<N>`) on its own branch, sharing the repo's
    object store. Separate HEAD + separate files ⇒ no checkout/HEAD race.
  - `release` (on finish, from the primary) frees the claim for the next session.

The claim is a directory published **atomically, fully populated** (meta.json
included) via a temp-dir-then-`rename` under the repo's *common* git dir
(`git rev-parse --git-common-dir`), which every worktree of the repo shares —
so the claim is visible from the primary checkout and from every linked
worktree (see `_publish_claim`; a bare `mkdir`-then-write-`meta.json` let two
racers both win). Exactly one racer wins the rename; the
rest fall through to worktree mode. A crashed session's claim is reclaimed
once it ages past the TTL (no fragile PID-liveness check on Windows).

Windows-specific by design (the fleet is Windows): worktree `.venv` is a
**junction** to the primary's `.venv` (`mklink /J`) — worktrees don't share
untracked files, and a 24-repo fleet can't recreate heavy venvs per worktree.
The teardown order is load-bearing: a junction MUST be stripped with
`rmdir` (reparse-safe, no `/s`) BEFORE `git worktree remove`, or git's recursive
delete follows the junction and wipes the *real* venv (proven the hard way; same
junction footgun as uninstall.ps1).

`.venv` isn't always the only gitignored path a repo's own gate needs — a
vendored install (`local-llm-hub`'s `vendor/comfyui/`), a model cache, and
similar never make it into a fresh worktree either, so that repo's own test
suite can never pass there even though the build itself succeeds.
A repo declares extra ones in its own `.fleet.toml`:

    [worktree]
    extra_junctions = ["vendor/comfyui"]

`worktree_junction_targets` reads this declaratively; `.venv` is always
junctioned first and remains the *only* target when no `.fleet.toml` /
`[worktree]` table is present, so an undeclared repo behaves exactly as
before. Every target — `.venv` and each declared extra — goes through the
same junction-then-reparse-safe-teardown path in `setup_worktree` /
`remove_worktree`; a declared path that doesn't exist in the primary is
simply skipped, never a setup failure.

`git worktree add` populates tracked files only, so a repo's own gitignored
runtime config (`config/webapp_config.json`, `config/apps.json`, ... whatever
each repo's own `config.json`-pattern requires) never makes it into the new
worktree. Left unfixed, an e2e suite that boots a disposable webapp+session-host
hits its own missing-config guard for nearly every test and mass-skips silently
-- the pre-ship gate still reports green. `setup_worktree`
copies the primary's `config/*.json` (excluding `*.sample.json` templates) into
the worktree right after the checkout is created. The primary's gitignored
`.env` is mirrored the same way, right after `config/*.json`: a repo whose
startup path reads a secret or setting from `.env` (e.g. `local-llm-hub`'s
`LOCAL_LLM_HUB_SSH_KEY`) otherwise stalls on the missing value until an
unrelated timeout trips the e2e stage red.

Some repos keep their runtime config at the repo **root** instead of under
`config/` (`accounting-quarterly`'s `config.json` and
`classification_rules.json`, `home-automation`'s `devices.json`). Neither
`copy_runtime_config` nor `copy_env_file` covers that shape, so a worktree for
one of these repos would crash on startup with no config at all.
`copy_root_config` closes it right after `.env`: rather than hardcode which
root filenames matter, it asks `git check-ignore` which of the repo's own
root-level `*.json` files are gitignored, so a repo that adds a new one is
covered with no code change here. Same rules as `copy_runtime_config` --
`*.sample.json` excluded, an existing destination left alone, a top-level
`port` repointed via `_repoint_config_port`, machine-bound values blanked via
`blank_machine_bound_config` -- reused rather than reinvented.

A byte-verbatim copy is not safe by itself: a repo's config can point at a
real, machine-bound sink outside the worktree -- a synced mirror/backup
folder, an index path, another repo's database -- and a worktree instance
booted for a "ready to validate" handoff then reads and writes the owner's
**real** data. `copy_runtime_config` blanks
those values right after the port rewrite, the same way and in the same
per-file loop. A repo declares exactly which dotted keys are machine-bound in
its own `.fleet.toml`:

    [worktree]
    blank_config_keys = ["mirror.dir", "mirror.backup_dir", "search.folder_roots"]

`worktree_blank_config_keys` reads this the same way `worktree_junction_targets`
reads `extra_junctions`. A repo that declares nothing (no `.fleet.toml`, no
`[worktree]` table, or the table without a `blank_config_keys` key) falls back
to a conservative built-in default: any string value, anywhere in the config,
that looks machine-bound -- an `{onedrive}`-style placeholder, or an absolute
Windows path -- is blanked, list entries filtered the same way. An empty
string is what the app already treats as "feature off" (task-os's mirror does
today); a repo whose own gate needs to keep a value opts out per key by
declaring `blank_config_keys` explicitly (an empty list disables the default
heuristic entirely). Independently of either mode, keys declared in
`[worktree] secret_config_keys` are removed from the copy outright
 -- see `worktree_config.py`'s docstring.

The port-allocation and config-blanking machinery behind the three `copy_*`
functions above lives in `worktree_config.py`, imported
and re-exported here so every existing caller keeps addressing it as
`worktree_claim.<name>`.

Subcommands:

  acquire <repo-root> [--issue N] [--branch B] [--ttl-hours H] [--force-worktree]
      Atomically claim the primary checkout. Prints `MODE=primary` (work in
      place) or `MODE=worktree` (caller then calls setup-worktree). Reclaims a
      claim older than the TTL, or one whose recorded branch no longer exists
      (merged-and-deleted => leaked claim, self-heals on the next acquire).

      Worktree mode is **forced**, with no primary attempt, whenever either
      `--force-worktree` is passed OR `APP_LAUNCHER_SESSION_ID` is set (an
      App-Launcher-dispatched session: a machine chose the work, not a human).
      Set `WORKTREE_CLAIM_ALLOW_PRIMARY=1` to defeat the environment trigger
      for a dispatched flow that genuinely must hold the primary; an explicit
      `--force-worktree` still wins over it. Enforcing this in the tool rather
      than in skill prose is deliberate -- a SKILL.md instruction alone was ignored
      by a dispatched worker inside the hour.

      `--force-worktree` skips the claim attempt entirely and always prints
      `MODE=worktree`. The claim only protects against a
      second *claiming session*; a live production process sitting in the same
      directory — the launcher webapp, home-automation's tray, or fleet-config's
      own hooks/ + skills/ junctioned into every live `~/.claude` — is not a
      claim holder, so a first-and-only unattended agent legitimately wins
      `MODE=primary` and edits files that a running app is serving. Unattended
      fleet-fanout dispatch (cleanup-fleet, cleanup-fleet-all, codebase-audit's
      security self-heal) therefore never works a primary checkout, for any
      repo. Interactive single-session `/issue-start` keeps the default
      claim-or-worktree behaviour.

  setup-worktree <repo-root> <issue-N> <branch>
      `git worktree add <repo>-wt-<N> <branch>` — created off the default
      branch, or off `origin/<branch>` when the branch is already pushed, or
      checked out in place when it already exists locally —
      + junction `.venv` and any repo-declared `[worktree] extra_junctions`
      into it, then copy the primary's gitignored `config/*.json` runtime
      config (excluding `*.sample.json` templates, which are already tracked)
      into the worktree, blanking machine-bound path values along the way
      (repo-declared `[worktree] blank_config_keys`, or a conservative
      default heuristic) and removing any repo-declared
      `[worktree] secret_config_keys` on top. Prints
      `WORKTREE=<path>`.

  release <repo-root>
      Remove the primary claim. Idempotent. (Worktree sessions never hold it.)

  assert-owner <repo-root> <issue-N>
      Guard before a primary-tree `git checkout <main>` (issue-finish step 5,
      issue-start step 4): refuses (exit 1) if the tree is dirty or the claim
      is held by a *different* issue; passes (exit 0) if the tree is clean and
      the claim is free or already owned by <issue-N>.

  land-primary <repo-root> <issue-N>
      Make a worktree lane's merge *live*: fast-forward the primary checkout,
      or report why it couldn't. Same guard as assert-owner (clean tree, claim
      free or owned by <issue-N>) plus "already on the default branch", then
      a guarded `merge --ff-only` (untrack_guard.py: every live file the merge
      untracks is kept) and a `rev-list --count
      HEAD..origin/<default>` check. Prints one `PRIMARY=live behind=0` /
      `PRIMARY=stale reason=<why>` line in every outcome -- the line the finish
      summary quotes -- plus `RESTORED_UNTRACKED=` / `KEPT_ASIDE=` lines when
      the guard kept a file. Exit 0 only when
      live. Never checks out, stashes or forces the primary: a refusal is
      reported, not recovered.

      A fourth guard refuses a tree a **live process is serving**:
      repos declaring `tray_cmd` in `hooks/projects.toml`
      run a long-lived server out of their checkout, and fast-forwarding one
      leaves static assets on disk ahead of the Python held in memory -- a UI
      served from two commits at once. `live_service_check` decides it,
      `probe_service` establishes it from the OS listener table -- both in
      `skills/_lib/worktree/service_probe.py`, imported here (the
      question is generic, the claim FSM is just its first caller). (Port absent
      from it = safely idle; unreadable table, or no port to probe = refuse,
      an unestablished fact is never a pass). There is no "landed anyway"
      variant: both landing and refusing need the same restart, and only
      landing serves mixed-commit assets meanwhile. `fleet-config` declares no `tray_cmd` and still lands
      unconditionally -- landing it is exactly what makes a merge live through
      the `~/.claude` junctions.

      That guard asks its question *after* "is there anything to pull?":
      a tree already level with its remote is landed by
      definition, so a service serving it is irrelevant and the answer is
      `PRIMARY=live behind=0`, exit 0 -- reporting `stale` for a demonstrably
      live deploy inverts the unknown-is-not-pass rule by downgrading an
      *established* fact. The count is taken against a freshly fetched remote
      ref; a fetch or count that fails leaves it unknown, which takes the
      ordinary path through the service guard rather than a shortcut.

  remove-worktree <worktree-path> [--force-nonstandard-name]
      Reparse-safe teardown: strip the .venv junction, then
      `git worktree remove --force` + `git worktree prune`. Tolerates a
      worktree that git already deregistered while its directory survived:
      it resolves the primary by inverting the
      `<repo>-wt-<N>` convention and falls back to a plain delete, always
      after the junction strip. Exits 1, naming the path, if the tree is
      still there afterwards (a live process is holding a file inside it);
      the caller must report that as residue, never as a clean teardown.
      The usual holder is a leaked e2e browser helper -- `remove_worktree`'s
      docstring names the sweep to run before retrying.

      Refuses (exit 2, deletes nothing) unless the target actually **is** a
      linked worktree: a primary checkout (`--git-common-dir` == `--git-dir`)
      is rejected outright, and a path whose basename doesn't match the
      `<repo>-wt-<N>` convention is rejected unless `--force-nonstandard-name`
      is passed. An agent that confuses this subcommand's `worktree_path` arg
      with every sibling subcommand's `repo_root` arg would have the
      primary checkout `rmtree`'d, destroying a repo's gitignored personal
      data that exists nowhere else.

  status <repo-root>
      Print the current claim holder (if any) and `git worktree list`.

stdlib + the `git` CLI only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The live-service capability lives in its own module --
# "is this repo's declared service listening, and at which sha?" is a generic
# question `/issue-finish` and any health sweep want, not part of the claim
# FSM. `land-primary` is only its first caller, so it imports the one verdict
# it needs rather than re-exporting the subsystem it no longer owns.
from fleet_core.utf8_stdio import ensure_utf8_stdio  # noqa: E402
from untrack_guard import guarded_fast_forward  # noqa: E402
from untrack_guard import report_lines as untrack_report_lines

# The claim FSM and the git/junction operations live in the `worktree`
# package; their names are re-exported so every caller and test keeps
# addressing them as `worktree_claim.<name>`. A test that swaps one of them
# out swaps it on the module that owns it.
from worktree.claim import (  # noqa: E402
    DEFAULT_TTL_HOURS,
    format_primary_state,
    land_primary_check,
    owner_check,
    read_meta,
    try_acquire,
)
from worktree.claim import (
    LOCK_NAME as LOCK_NAME,
)
from worktree.claim import (
    META_NAME as META_NAME,
)
from worktree.claim import (
    is_stale as is_stale,
)
from worktree.claim import (
    worktree_path as worktree_path,
)
from worktree.claim import (
    write_meta as write_meta,
)
from worktree.gitops import (  # noqa: E402
    _common_dir_or_none,
    _git,
    _is_primary_checkout_safe,
    branch_exists_on_remote,
    count_behind,
    is_primary_checkout,
    lock_dir_for,
    main_ref,
    mode_check,
    primary_for_worktree,
    remove_worktree,
    setup_worktree,
    worktree_junction_targets,
)
from worktree.gitops import (
    _junction as _junction,
)
from worktree.gitops import (
    _looks_like_worktree_name as _looks_like_worktree_name,
)
from worktree.gitops import (
    _strip_junction as _strip_junction,
)
from worktree.gitops import (
    common_dir as common_dir,
)
from worktree.gitops import (
    worktree_add_args as worktree_add_args,
)
from worktree.service_probe import service_state  # noqa: E402

# Worktree runtime-config provisioning (ports + machine-bound value blanking)
# lives in its own module -- including the side-instance preflight's own
# config discovery and
# re-verification (`check_provisioned_configs`), which reuses that module's
# own candidate discovery and blanking helpers rather than reaching into them
# from here. Only public names are imported; the public ones are also what
# tests address as `worktree_claim.<name>`. Import only what has a caller.
from worktree.worktree_config import (  # noqa: E402
    WT_PORT_BASE as WT_PORT_BASE,
)
from worktree.worktree_config import (
    WT_PORT_SPAN as WT_PORT_SPAN,
)
from worktree.worktree_config import (
    check_provisioned_configs,
    port_registry_state,
    worktree_blank_config_keys,
    worktree_primary_instance_ok,
    worktree_read_safe_config_keys,
)
from worktree.worktree_config import (
    copy_env_file as copy_env_file,
)
from worktree.worktree_config import (
    copy_root_config as copy_root_config,
)
from worktree.worktree_config import (
    copy_runtime_config as copy_runtime_config,
)
from worktree.worktree_config import (
    remove_secret_config as remove_secret_config,
)
from worktree.worktree_config import (
    worktree_port as worktree_port,
)
from worktree.worktree_config import (
    worktree_secret_config_keys as worktree_secret_config_keys,
)
from worktree_residue import WT_SEP as WT_SEP  # noqa: E402

ensure_utf8_stdio()


# ---- CLI ------------------------------------------------------------------

def _resolve_path_arg(arg: str) -> Optional[Path]:
    """Resolve a path-ish CLI arg, tolerating the bare-name forms agents pass.

    Returns the resolved existing path, or None if nothing plausible exists.
    Three shapes are accepted: an ordinary relative/absolute path; the current
    repo's own name from inside it (-> CWD); and a sibling
    worktree name from the parent repo (-> CWD.parent/<name>,
    e.g. `remove-worktree fleet-config-wt-7` run from inside `fleet-config`).
    """
    repo = Path(arg).resolve()
    if repo.exists():
        return repo
    # Only a bare, non-absolute name (no directory components) gets the fallback.
    p = Path(arg)
    if not p.is_absolute() and p.parent == Path("."):
        cwd = Path.cwd()
        if cwd.name.casefold() == arg.casefold():
            return cwd
        sibling = cwd.parent / arg
        if sibling.exists():
            return sibling
    return None


def _resolve_repo(arg: str) -> Path:
    resolved = _resolve_path_arg(arg)
    if resolved is None:
        sys.exit(f"No such repo path: {Path(arg).resolve()}")
    return resolved


def worktree_forced(argv_flag: bool, env: "Mapping[str, str]") -> Tuple[bool, str]:
    """Decide whether this `acquire` may return `MODE=primary` at all.

    Returns `(forced, why)`. Two independent triggers, either sufficient:

      - the caller passed `--force-worktree` (unattended fleet fanout asking
        for it explicitly), or
      - `APP_LAUNCHER_SESSION_ID` is set, meaning App Launcher's Board spawned
        this session: a machine dispatched the work and no human chose the tree.

    The second trigger exists because the first one *did not hold* in practice.
    A line of `/issue-start` SKILL.md prose telling the agent to pass the flag
    was ignored: within the hour a dispatched
    worker landed in a primary checkout anyway with everything wired correctly —
    the variable set, the junctioned skill updated, the flow chained. A rule an
    agent has to read, recognise and choose to obey is advisory; every other
    guarantee in this lifecycle (the claim FSM, the junction-strip ordering, the
    halt gate) is enforced by code that cannot be skipped, and so is this now.

    `WORKTREE_CLAIM_ALLOW_PRIMARY=1` is the deliberate escape hatch, for the
    rare dispatched flow that genuinely must hold the primary. It only defeats
    the environment trigger — an explicit `--force-worktree` still wins, since
    a caller that asked for isolation by name should get it.
    """
    if argv_flag:
        return True, "--force-worktree"
    if env.get("APP_LAUNCHER_SESSION_ID") and env.get("WORKTREE_CLAIM_ALLOW_PRIMARY") != "1":
        return True, "APP_LAUNCHER_SESSION_ID (launcher-dispatched; fleet-config#525)"
    return False, ""


# ---- side-instance preflight ---------------------------

SIDE_OK = "ok"
SIDE_REFUSED = "refused"
SIDE_UNKNOWN = "unknown"


def side_instance_decision(is_primary: Optional[bool], env: "Mapping[str, str]",
                           declared_ok: bool) -> Tuple[str, str]:
    """May an app instance be booted out of this checkout? `(state, reason)`.

    The pure half of `side-instance-preflight`. Three states, because the
    dangerous one is the third: `is_primary=None` means we could not establish
    which kind of checkout this is, and a gate that cannot establish a fact
    reports `unknown` rather than folding it into the pass.

    Why this exists at all, given `worktree_forced` already refuses a primary
    checkout: that refusal runs inside `acquire`, and `acquire` is a step a
    lane can simply not take. A free-text-dispatched lane that never invokes
    `/issue-start` runs `git checkout -b` in whatever tree it woke up in, and
    nothing ever asks. `branch_before_edit_guard` does not catch it either --
    it blocks edits on the *default branch*, and the lane is on a feature
    branch. So the live app served an hour of uncommitted JS and CSS to a
    phone, with every guard intact and none of them consulted.
    Booting is the one step a lane that wants a side
    instance cannot skip, so the check belongs here too.

    Two escapes, and both are deliberate rather than accidental:

      - `WORKTREE_CLAIM_ALLOW_PRIMARY=1`, the same environment hatch
        `worktree_forced` honours -- one spelling of "this session means it".
      - a repo's own `[worktree] primary_instance_ok = true`, for a repo whose
        operational lanes genuinely belong in the primary clone (`life-os`,
        whose gitignored identity/context/state exist nowhere else). That
        exception previously lived only in whichever session remembered to
        export the variable.
    """
    if is_primary is None:
        return SIDE_UNKNOWN, "cannot tell whether this is a primary checkout or a worktree"
    if not is_primary:
        return SIDE_OK, "linked worktree"
    if declared_ok:
        return SIDE_OK, "primary checkout, allowed by [worktree] primary_instance_ok"
    if env.get("WORKTREE_CLAIM_ALLOW_PRIMARY") == "1":
        return SIDE_OK, "primary checkout, allowed by WORKTREE_CLAIM_ALLOW_PRIMARY=1"
    return SIDE_REFUSED, (
        "primary checkout — booting here serves uncommitted work to whatever "
        "depends on the live instance (fleet-config#937). Build in a worktree "
        "(`setup-worktree`), or declare [worktree] primary_instance_ok = true "
        "if this repo's lanes genuinely belong in the primary"
    )


def _primary_or_none(path: Path) -> Optional[bool]:
    """`is_primary_checkout(path)`, or `None` when git cannot answer.

    Distinct from `_is_primary_checkout_safe`, whose `False` is right for
    `remove_worktree` (a non-repo is not a primary checkout, and the naming
    guard catches it next). Here `False` would be a licence to boot, so an
    unanswerable probe has to stay unanswered.
    """
    try:
        return is_primary_checkout(path)
    except (subprocess.CalledProcessError, OSError):
        return None


def cmd_side_instance_preflight(args: argparse.Namespace) -> int:
    """Refuse-or-report before a lane boots an app instance out of a checkout.

    Exit 0 = safe to boot, 1 = refused, 2 = unknown. `unknown` is never
    permission: it means the preflight could not establish a fact it needs, and
    the caller has to resolve that rather than read silence as consent.

    It has exactly one side effect: it **reserves** each provisioned config's
    port for this checkout. That is deliberate -- a preflight that only
    observed an unreserved port would name the collision risk without removing
    it, and every worktree provisioned before reservations existed would report
    `unknown` forever. Refusing is still the answer when another live worktree
    already holds the port.

    Beyond that it reports; it does not boot. Starting the app is
    repo-specific -- a tray batch file, uvicorn, Streamlit -- and a `stand-up`
    command that could not actually start most repos would be the helper that
    looks total and isn't. What a lane still owns after a green preflight is
    printed with the report, not left implied.
    """
    checkout = _resolve_path_arg(args.checkout or ".")
    if checkout is None:
        print(f"SIDE_INSTANCE=unknown reason=no such path: "
              f"{Path(args.checkout or '.').resolve()}")
        return 2
    state, reason = side_instance_decision(
        _primary_or_none(checkout), os.environ, worktree_primary_instance_ok(checkout))
    print(f"SIDE_INSTANCE={state} checkout={checkout} reason={reason}")
    if state != SIDE_OK:
        return 1 if state == SIDE_REFUSED else 2

    declared_keys = worktree_blank_config_keys(checkout)
    read_safe_keys = worktree_read_safe_config_keys(checkout)
    if read_safe_keys:
        print(f"CONFIG_READ_SAFE={len(read_safe_keys)}: {', '.join(read_safe_keys)}")
    else:
        print("CONFIG_READ_SAFE=0: no [worktree] read_safe_config_keys declared — "
              "every machine-bound value stays blank")

    registry = port_registry_state()
    failures: List[str] = []
    unknowns: List[str] = []
    if registry != "ok":
        unknowns.append("the port reservation registry could not be read")
        print("PORT_REGISTRY=unknown: reservations not established")

    # Discovery, the unsafe-key re-check and the port-reservation loop all
    # live next to `copy_runtime_config`/`copy_root_config` now, so the
    # checked configs can never drift from what a real setup provisions.
    config_failures, config_unknowns = check_provisioned_configs(
        checkout, declared_keys, read_safe_keys)
    failures.extend(config_failures)
    unknowns.extend(config_unknowns)

    # Only meaningful for a worktree, and only for a target the primary
    # actually has: `setup_worktree` deliberately skips a declared-but-absent
    # one, so a repo with no `.venv` at all is not misconfigured. What matters
    # is a target that exists in the primary and did NOT make it across --
    # app-launcher's gitignored `webapp/certificates/`, whose absence produces
    # a browser warning that reads like a bug in the change under test.
    primary = primary_for_worktree(checkout)
    if primary is not None and primary.resolve() != checkout.resolve():
        for rel in worktree_junction_targets(checkout):
            if (primary / rel).is_dir() and not (checkout / rel).exists():
                print(f"JUNCTION_MISSING={rel}")
                failures.append(f"declared [worktree] target {rel} exists in the "
                                f"primary but is missing here — the app will behave "
                                f"as if it were never configured")

    print("STILL_YOURS: start the app yourself (repo-specific); point the browser at "
          "the port above, not the primary's; do not commit the provisioned config.")
    if failures:
        for line in failures:
            print(f"REFUSED: {line}")
        return 1
    if unknowns:
        for line in unknowns:
            print(f"UNKNOWN: {line}")
        return 2
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo_root)
    forced, why = worktree_forced(getattr(args, "force_worktree", False), os.environ)
    if forced:
        args.force_worktree = True
        if why != "--force-worktree":
            print(f"# forced to worktree mode by {why}", file=sys.stderr)
    if getattr(args, "force_worktree", False):
        # No claim attempt at all: unattended fanout must never touch a primary
        # checkout, even an unclaimed one (a live app or a
        # live junction is not a claim holder). Deliberately does not publish a
        # claim either, so the primary stays free for a human session.
        print("MODE=worktree")
        print("# --force-worktree: primary checkout not eligible for unattended "
              "fanout; caller must run setup-worktree", file=sys.stderr)
        return 0
    lock = lock_dir_for(repo)
    meta = {
        "created": time.time(),
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "issue": args.issue,
        "branch": args.branch,
        "repo": str(repo),
    }
    mode, holder = try_acquire(
        lock, meta, time.time(), args.ttl_hours,
        branch_exists=lambda b: branch_exists_on_remote(repo, b),
    )
    print(f"MODE={mode}")
    if mode == "worktree" and holder:
        print(f"# primary held since {holder.get('created_iso', '?')} "
              f"by issue {holder.get('issue', '?')} on {holder.get('branch', '?')}",
              file=sys.stderr)
    return 0


def cmd_setup_worktree(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo_root)
    wt = setup_worktree(repo, args.issue, args.branch)
    print(f"WORKTREE={wt}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo_root)
    lock = lock_dir_for(repo)
    if lock.exists():
        shutil.rmtree(lock, ignore_errors=True)
        print(f"Released claim on {repo}")
    else:
        print(f"No claim to release on {repo}")
    return 0


def cmd_remove_worktree(args: argparse.Namespace) -> int:
    # Tolerate the bare sibling-name form. When nothing
    # resolves, hand the literal resolved path to remove_worktree so it prints
    # its honest "already gone" message rather than exiting.
    resolved = _resolve_path_arg(args.worktree_path)
    return remove_worktree(
        resolved or Path(args.worktree_path).resolve(),
        force_nonstandard_name=args.force_nonstandard_name,
    )


def cmd_assert_owner(args: argparse.Namespace) -> int:
    """Refuse a `git checkout <main>` unless it's safe for `<issue>` to run it.

    Called immediately before the checkout in `/issue-finish` step 5 and
    `/issue-start` step 4 — the claim system routes a
    *second* session into a worktree, but says nothing about whether the
    process about to switch the primary tree's HEAD is actually the claim
    holder. Exits 0 (`ASSERT_OWNER=pass`) when the tree is clean and the claim
    is free or owned by this issue; exits 1 (`ASSERT_OWNER=refuse: <reason>`)
    otherwise, printing the same "primary held since ..." shape the claim
    system already uses so a human or chief sees why the checkout stopped.
    """
    repo = _resolve_repo(args.repo_root)
    lock = lock_dir_for(repo)
    holder = read_meta(lock) if lock.exists() else None
    dirty = bool(_git(repo, "status", "--porcelain").stdout.strip())
    ok, reason = owner_check(holder, args.issue, dirty)
    if ok:
        print(f"ASSERT_OWNER=pass ({reason})")
        return 0
    print(f"ASSERT_OWNER=refuse: {reason}", file=sys.stderr)
    return 1


def cmd_land_primary(args: argparse.Namespace) -> int:
    """Fast-forward the primary checkout after a worktree lane's merge, or say
    plainly that it didn't.

    Prints exactly one `PRIMARY=` line on stdout in every outcome -- that line
    is what the finish summary quotes. Exit 0 only for `PRIMARY=live behind=0`;
    1 for any stale state; 2 when the target isn't a primary checkout at all.
    The distance from the remote is counted (against a fresh fetch) *before*
    the guards, because "there is nothing to pull" answers the whole question:
    a level tree lands as a no-op, service or no service, and the pull is
    skipped entirely.

    Never checks out, stashes, forces, or otherwise "recovers" a tree it was
    refused: reporting stale *is* the correct outcome, and `--ff-only` fails
    safely rather than merging, so nothing here can lose work. Nor does it
    restart anything for the repos the live-service guard refuses -- restarts on this
    machine are confirmation-gated and operator-owned; this stops *creating*
    the skew and says what unblocks it.
    """
    code, lines = land_primary(_resolve_repo(args.repo_root), args.issue)
    for line in lines:
        print(line)
    return code


def land_primary(repo: Path, issue: str,
                 service: Optional[Tuple[bool, str]] = None) -> Tuple[int, List[str]]:
    """The body of `land-primary`: `(exit_code, stdout_lines)`, nothing printed.

    `service` replaces the live-service guard's verdict for a caller that owns
    the restart itself (`vendored_wave/primaries.py`): landing then restarts
    straight away, so the mixed-commit window the guard exists to refuse is
    one command long. `None` probes the machine, exactly as the CLI does.
    """
    if not _is_primary_checkout_safe(repo):
        return 2, [f"PRIMARY=stale reason=not a primary checkout: {repo}"]
    lock = lock_dir_for(repo)
    holder = read_meta(lock) if lock.exists() else None
    dirty = bool(_git(repo, "status", "--porcelain").stdout.strip())
    current = _git(repo, "branch", "--show-current").stdout.strip()
    ref = main_ref(repo)                                   # e.g. 'origin/main'
    main_branch = ref.split("/", 1)[1] if "/" in ref else ref
    behind = count_behind(repo, ref)
    ok, reason = land_primary_check(holder, issue, dirty, current, main_branch,
                                    behind, service if service is not None else service_state(repo))
    if not ok:
        return 1, [format_primary_state(False, reason)]
    if behind == 0:
        # Nothing to fast-forward -- the tree is already what the merge
        # produced, so there is no pull to run and no service to skew.
        return 0, [format_primary_state(True, reason, 0)]
    # A plain `pull --ff-only` deletes every live file the merge untracks,
    # ignored or not -- the guard keeps and reports them.
    ff = guarded_fast_forward(repo, ref)
    if not ff.ok:
        return 1, [format_primary_state(False, f"fast-forward failed: {ff.detail}")]
    behind = count_behind(repo, ref, fetch=False)           # count_behind fetched above
    lines = [format_primary_state(True, reason, behind), *untrack_report_lines(ff)]
    return (0 if behind == 0 else 1), lines


def cmd_mode(args: argparse.Namespace) -> int:
    """Print `primary` or `worktree` for the checkout the caller is *standing in*
    — the deterministic decision /issue-finish keys its teardown on.

    The answer is about the **cwd**, never about `repo_root`.
    `repo_root` only *identifies* which repo the cwd is expected to belong to;
    it does not select a tree. That distinction is the entire bug: every skill
    documents the call as `mode <repo>` with a bare name, and from inside
    `<repo>-wt-<N>` the sibling fallback in `_resolve_path_arg` (right for
    `remove-worktree`) resolves that name to the *primary*. The old
    implementation then asked `is_primary_checkout` about the primary and
    truthfully answered `primary` — to a worktree lane. Silent, and always wrong
    in the dangerous direction: the primary teardown path runs `gh pr merge
    --delete-branch` and hits `'main' is already used by worktree`.

    An argument that cannot be reconciled with the cwd prints `UNKNOWN
    reason=<why>` and exits 2 — a helper that cannot establish which checkout it
    was asked about says so rather than guessing.
    """
    resolved = _resolve_path_arg(args.repo_root)
    if resolved is None:
        print(f"UNKNOWN reason=no such repo path: {Path(args.repo_root).resolve()}")
        return 2
    cwd = Path.cwd()
    ok, reason = mode_check(_common_dir_or_none(cwd), _common_dir_or_none(resolved))
    if not ok:
        print(f"UNKNOWN reason={reason}")
        return 2
    print("primary" if is_primary_checkout(cwd) else "worktree")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo_root)
    lock = lock_dir_for(repo)
    holder = read_meta(lock) if lock.exists() else None
    if holder:
        print(f"CLAIM=held  issue={holder.get('issue')}  branch={holder.get('branch')}  "
              f"since={holder.get('created_iso')}")
    else:
        print("CLAIM=free")
    print(_git(repo, "worktree", "list", check=False).stdout.strip())
    return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="worktree_claim", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("acquire", help="atomically claim the primary checkout")
    a.add_argument("repo_root")
    a.add_argument("--issue", default=None)
    a.add_argument("--branch", default=None)
    a.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    a.add_argument("--force-worktree", action="store_true",
                   help="never claim the primary; always print MODE=worktree "
                        "(unattended fleet fanout, fleet-config#515)")
    a.set_defaults(func=cmd_acquire)

    s = sub.add_parser("setup-worktree", help="create the sibling worktree + junction .venv")
    s.add_argument("repo_root")
    s.add_argument("issue")
    s.add_argument("branch")
    s.set_defaults(func=cmd_setup_worktree)

    r = sub.add_parser("release", help="release the primary claim (idempotent)")
    r.add_argument("repo_root")
    r.set_defaults(func=cmd_release)

    rw = sub.add_parser("remove-worktree", help="reparse-safe worktree teardown")
    rw.add_argument("worktree_path")
    rw.add_argument("--force-nonstandard-name", action="store_true",
                     help="skip the <repo>-wt-<N> naming-convention guard "
                          "(fleet-config#589)")
    rw.set_defaults(func=cmd_remove_worktree)

    ao = sub.add_parser("assert-owner", help="refuse a main checkout unless it's safe for <issue>")
    ao.add_argument("repo_root")
    ao.add_argument("issue")
    ao.set_defaults(func=cmd_assert_owner)

    lp = sub.add_parser("land-primary",
                        help="fast-forward the primary after a worktree merge, or report stale")
    lp.add_argument("repo_root")
    lp.add_argument("issue")
    lp.set_defaults(func=cmd_land_primary)

    md = sub.add_parser("mode",
                        help="print 'primary' or 'worktree' for the cwd checkout "
                             "(run it from the checkout you are asking about)")
    md.add_argument("repo_root",
                    help="the repo the cwd must belong to — it identifies the repo, "
                         "it does NOT select a tree; a mismatch prints "
                         "'UNKNOWN reason=...' and exits 2 (fleet-config#652)")
    md.set_defaults(func=cmd_mode)

    si = sub.add_parser("side-instance-preflight",
                        help="refuse-or-report before booting an app instance out "
                             "of a checkout (0 ok / 1 refused / 2 unknown)")
    si.add_argument("checkout", nargs="?", default=".",
                    help="the checkout the instance would boot from (default: cwd)")
    si.set_defaults(func=cmd_side_instance_preflight)

    st = sub.add_parser("status", help="show claim holder + worktree list")
    st.add_argument("repo_root")
    st.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
