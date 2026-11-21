"""Worktree runtime-config provisioning: ports, machine-bound value blanking,
removal of repo-declared secret keys, and the machine-global port reservation
those ports are handed out under.

Split out of `worktree_claim.py` -- port allocation and
config blanking share no state with the claim FSM, junction teardown, or the
primary-landing guard that module owns, and are exercised by their own tests.

`git worktree add` populates tracked files only, so a repo's own gitignored
runtime config (`config/*.json`, root-level `*.json` such as
`accounting-quarterly`'s `config.json`, and `.env`) never makes it into a
fresh worktree. Left unfixed, an e2e suite that boots a disposable
webapp+session-host hits its own missing-config guard for nearly every test
and mass-skips silently -- the pre-ship gate still reports green.
`copy_runtime_config` / `copy_root_config` / `copy_env_file` close that gap;
`worktree_claim.setup_worktree` calls all
three right after the checkout is created.

A byte-verbatim copy is not safe by itself: a repo's config can point at a
real, machine-bound sink outside the worktree -- a synced mirror/backup
folder, an index path, another repo's database -- and a worktree instance
booted for a "ready to validate" handoff then reads and writes the owner's
**real** data. `blank_machine_bound_config`
blanks those values right after the port rewrite. A repo declares exactly
which dotted keys are machine-bound in its own `.fleet.toml`:

    [worktree]
    blank_config_keys = ["mirror.dir", "mirror.backup_dir", "search.folder_roots"]

`worktree_blank_config_keys` reads this the same way `worktree_claim`'s
`worktree_junction_targets` reads `extra_junctions`. A repo that declares
nothing falls back to a conservative built-in default (`_blank_default_heuristic`):
any string value, anywhere in the config, that looks machine-bound -- an
`{onedrive}`-style placeholder, or an absolute Windows path -- is blanked,
list entries filtered the same way.

"Byte-verbatim apart from the rewrites" is therefore no longer the whole
contract: a repo can also declare keys whose values must
never be reproduced in a worktree copy at all -- a live credential, a secrets
sub-table:

    [worktree]
    secret_config_keys = ["auth.token", "credentials"]

`worktree_secret_config_keys` reads it; `remove_secret_config` deletes each
declared dotted key outright (any value type) ahead of the blanking pass. It is
additive, not a mode: it never switches off the default heuristic the way
declaring `blank_config_keys` does, and composes with an explicit
`blank_config_keys` just the same. Undeclared, it removes nothing, so every
other repo's copy is unchanged. Scope is the JSON configs these copy
functions rewrite; `.env` is still copied verbatim.

Blanking is what stops a worktree instance writing to live state, but it is
blunt: every machine-bound-looking value goes, including the ones the app only
ever *reads*, and a lane standing a side instance up then has to put some back
by hand. Which ones are safe to restore was per-lane judgement, and restoring
a **write** path silently points a test instance at the owner's live state --
the exact failure blanking exists to prevent. A repo now
declares the read-only ones itself:

    [worktree]
    read_safe_config_keys = ["projects_dir", "apps_scan_root"]

`worktree_read_safe_config_keys` reads it. It is **additive** like
`secret_config_keys`, never a mode switch, and it **fails closed**: blanking
stays the default and exemption is opt-in, so an undeclared key -- including
one added to a config after the declaration was written -- is blanked, never
assumed safe. An explicit `blank_config_keys` entry wins over a
`read_safe_config_keys` entry for the same key, for the same reason; in
practice declaring `blank_config_keys` turns the heuristic off entirely, and
the exemption only ever modulates the heuristic.

Carrying the primary's port across into a copied config is what made every
worktree lane's e2e suite report a collision with the user's live tray and
refuse to run -- a false positive, since the suite boots its own disposable
instance on a free port and never touches the tray's.
`worktree_port` deterministically assigns each copied config its own port in
the `8500-8999` band, seeded from the issue number so re-running setup for
the same lane reproduces the same port.

Deterministic-and-probed was still not exclusive: the probe
only sees a port something is **listening** on, and at provisioning time
nothing has booted yet, so two worktrees set up back to back could be written
the same port and collide later as a bind error that never mentions the other
lane. Ports are therefore *reserved* as they are handed out, in one
machine-global registry under `hooks_state.state_dir()` -- see
`reserve_port`. Liveness is the worktree directory itself, not a TTL: an entry
whose directory is gone is pruned, so a crashed lane cannot strand a port and
a slow one cannot lose its own.

This module is deliberately import-free of `worktree_claim.py` (which imports
*this* module, re-exporting the names its existing callers and tests use) --
the worktree-suffix separator comes from the `worktree_residue` leaf module,
and only the `_git` wrapper is duplicated here in miniature, to keep the
dependency a one-way DAG.

stdlib + the `git` CLI only.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import git_run  # noqa: E402

# The read-modify-write transaction primitives for a shared `hooks/state/`
# JSON file are `active_issue.py`'s -- the lock,
# the target-named atomic temp and the tolerant read. Reused rather than
# re-derived: a ninth hand-rolled copy is exactly what `hooks_state.py`'s own
# docstring says goes wrong. `active_issue` imports neither this module nor
# `worktree_claim`, so the one-way DAG this module's docstring describes is
# unaffected.
from active_issue import read_rows, state_lock, write_rows  # noqa: E402
from fleet_core import (
    fleet_toml,  # noqa: E402
    runtime_files,  # noqa: E402
)
from fleet_core.hooks_state import state_dir  # noqa: E402

# worktree_residue is a leaf (git_run only), so this module stays import-free
# of worktree_claim.py, per the module docstring above.
from worktree_residue import WT_SEP  # noqa: E402

# Band a worktree's copied runtime-config ports are repointed into.
# Rationale for these exact bounds is in `worktree_port`'s docstring.
WT_PORT_BASE = 8500
WT_PORT_SPAN = 500

# Loopback connect timeout for `_port_is_free`, a stated bound rather than a
# measured one: a local connect succeeds or is refused almost at once, so 0.2 s
# is a generous ceiling. It decides a fact -- a connect that times out is a
# non-zero `connect_ex`, so a listener too backed up to accept within it reads as free.
PORT_PROBE_TIMEOUT_S = 0.2


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return git_run.run_git(["-C", str(repo), *args], check=check)


def _port_is_free(port: int) -> bool:
    """True if nothing is listening on 127.0.0.1:`port` right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(PORT_PROBE_TIMEOUT_S)
        return sock.connect_ex(("127.0.0.1", port)) != 0


# ---- machine-global port reservations -------------------

# One registry for the whole machine, not per repo: the 8500-8999 band is
# shared by every fleet repo's worktrees, so two lanes in *different* repos can
# collide just as easily as two in the same one.
PORT_REGISTRY_FILENAME = "worktree-ports.json"


def port_registry_path() -> Path:
    """The reservation registry, resolved at call time so `CLAUDE_HOOKS_STATE_DIR`
    still redirects it (hermetic test runs must never write live fleet state)."""
    return state_dir() / PORT_REGISTRY_FILENAME


def _live_rows(rows: dict) -> dict:
    """`rows` minus every reservation whose worktree directory is gone.

    The worktree directory **is** the liveness evidence -- stronger than the
    TTL the claim lock has to fall back on, because a worktree that still
    exists is a lane that may still boot, however long it has been quiet, and
    one that has been torn down can never bind its port again. A malformed row
    (no `worktree`, not an object) is dropped for the same reason a null probe
    is not a pass: it cannot establish that anything holds the port.
    """
    live = {}
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        holder = row.get("worktree")
        if not isinstance(holder, str) or not holder:
            continue
        try:
            if not Path(holder).is_dir():
                continue
        except (OSError, ValueError):
            # ValueError: an embedded null or otherwise unrepresentable path
            # makes `os.stat` raise rather than return False.
            continue
        live[str(key)] = row
    return live


def port_reservations() -> dict:
    """Live reservations as `{port: row}`, pruned, keyed by `int`.

    Read-only and never raises -- an unreadable or malformed registry reads as
    "nothing reserved", the same fail-open contract the rest of this module's
    reads use. Callers that need to know the registry could not be *consulted*
    (the side-instance preflight) use `port_registry_state` instead.
    """
    live = _live_rows(read_rows(port_registry_path()))
    out = {}
    for key, row in live.items():
        try:
            out[int(key)] = row
        except (TypeError, ValueError):
            continue
    return out


def port_registry_state() -> str:
    """`"ok"` if the registry was readable, `"unknown"` if it could not be.

    A registry we failed to read is not an empty one. Provisioning treats the
    two the same (a broken state file must not fail a worktree setup), but a
    gate that reports "no collision" has to be able to say it never
    established that -- unknown is its own state, never folded into the pass.
    """
    path = port_registry_path()
    if not path.exists():
        return "ok"
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return "unknown"
    try:
        data = json.loads(raw)
    except ValueError:
        return "unknown"
    return "ok" if isinstance(data, dict) else "unknown"


def reserve_port(port: int, worktree: Path, config_name: str = "") -> Optional[str]:
    """Reserve `port` for `worktree`. `None` on success, else the holder's path.

    Idempotent for the same worktree, so re-running setup for a lane
    re-confirms its own reservation rather than colliding with itself. Dead
    reservations are pruned inside the same locked transaction, so the prune
    can never race a concurrent reserve.

    Raises `OSError` when the registry cannot be *read* as well as when it
    cannot be written. Reading it is fail-open everywhere else -- a broken
    state file must not fail a worktree setup -- but a write cannot be: writes
    are whole-file, and `read_rows` answers an unparseable registry with `{}`,
    so proceeding would replace every other lane's live reservation with this
    one. Refusing leaves the two callers on paths they already have (setup
    warns and uses the port unreserved; the preflight reports `unknown`), and
    leaves the file for a human to look at.
    """
    path = port_registry_path()
    holder = str(Path(worktree).resolve())
    with state_lock(path):
        if port_registry_state() != "ok":
            raise OSError(f"port registry is unreadable, refusing to overwrite it: {path}")
        rows = _live_rows(read_rows(path))
        existing = rows.get(str(port))
        if existing is not None and existing.get("worktree") != holder:
            return str(existing.get("worktree"))
        rows[str(port)] = {
            "worktree": holder,
            "config": config_name,
            "reserved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        write_rows(path, rows)
    return None


def release_ports(worktree: Path) -> list:
    """Drop every reservation held by `worktree`. Returns the released ports.

    Called from `worktree_claim.remove_worktree` so a torn-down lane hands its
    ports back immediately instead of waiting for the next reader to notice the
    directory is gone. Advisory: an unwritable registry is reported as nothing
    released rather than failing a teardown, and `_live_rows` would have pruned
    the rows anyway.
    """
    path = port_registry_path()
    holder = str(Path(worktree).resolve())
    released = []
    try:
        with state_lock(path):
            rows = _live_rows(read_rows(path))
            kept = {}
            for key, row in rows.items():
                if row.get("worktree") == holder:
                    try:
                        released.append(int(key))
                    except (TypeError, ValueError):
                        pass
                else:
                    kept[key] = row
            if released or kept != rows:
                write_rows(path, kept)
    except (OSError, TimeoutError):
        return []
    return sorted(released)


def worktree_port(issue: str, taken: "set" = frozenset(),
                  owner: Optional[Path] = None) -> int:
    """A port for a worktree's copied runtime config, in `8500-8999`.

    Deterministic first, honest second: the issue number seeds the offset so
    re-running setup for the same lane reproduces the same port, then we probe
    upward (wrapping inside the band) past anything already listening, already
    handed out to a sibling config file in this same worktree, or already
    **reserved** by another live worktree. A repo with two ported configs
    (app-launcher's webapp + session-host) therefore gets two distinct ports
    rather than one collided pair, and two lanes provisioned before either
    boots get two distinct ports rather than a later bind error.

    `owner` is the worktree this port is being computed for; its own
    reservations are not obstacles, so re-running setup for the same lane still
    reproduces the same port. Omitting it treats every reservation as taken,
    which is the conservative reading for a caller that is only asking.

    The band is chosen to clear three things at once: the fleet's own app ports
    (`844x`), this machine's known fixed listeners (cloudflared 20241-3,
    tailscaled 40746, OneDrive 42050, MouseWithoutBorders 15100/1, llama-server
    18093, StreamDeck 28196/8, MSI 26822/32683/33683, logioptionsplus 19010,
    hwinfo 10000), and the Windows ephemeral range 49152-65535.
    """
    own = str(Path(owner).resolve()) if owner is not None else None
    reserved = {
        port for port, row in port_reservations().items()
        if own is None or row.get("worktree") != own
    }
    digits = "".join(ch for ch in issue if ch.isdigit())
    seed = int(digits) if digits else sum(ord(ch) for ch in issue)
    for step in range(WT_PORT_SPAN):
        port = WT_PORT_BASE + ((seed + step) % WT_PORT_SPAN)
        if port not in taken and port not in reserved and _port_is_free(port):
            return port
    raise RuntimeError(
        f"no free port in {WT_PORT_BASE}-{WT_PORT_BASE + WT_PORT_SPAN - 1} "
        f"for worktree issue {issue!r}"
    )


def _allocate_port(issue: str, taken: "set", wt: Path, config_name: str) -> int:
    """`worktree_port` plus the reservation, retried past a lost race.

    `worktree_port` reads the registry and `reserve_port` writes it, so a
    sibling lane can slip between the two. The loser adds the contested port to
    its own `taken` set and asks again rather than failing -- the registry is
    the arbiter, and exactly one of the racers keeps each port.

    A registry that cannot be written at all is reported on stderr and the port
    is used unreserved: a worktree setup must not die because a state file is
    unavailable. The side-instance preflight is where that degradation becomes
    visible, as `unknown` rather than a silent pass.
    """
    blocked = set(taken)
    for _ in range(WT_PORT_SPAN):
        port = worktree_port(issue, blocked, owner=wt)
        try:
            holder = reserve_port(port, wt, config_name)
        except (OSError, TimeoutError) as exc:
            # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
            print(f"WORKTREE_PORT_RESERVE=unavailable ({exc.__class__.__name__}): "
                  f"{port} used without a reservation ({config_name})", file=sys.stderr)
            return port
        if holder is None:
            return port
        blocked.add(port)
    raise RuntimeError(
        f"no reservable port in {WT_PORT_BASE}-{WT_PORT_BASE + WT_PORT_SPAN - 1} "
        f"for worktree issue {issue!r}"
    )


def _load_json_object(dst: Path) -> Optional[dict]:
    """`dst` parsed as a JSON object, or `None` if it isn't one or doesn't
    parse -- the fail-open read every post-copy rewrite shares."""
    try:
        raw = json.loads(dst.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _repoint_config_port(dst: Path, wt: Path, taken: "set") -> Optional[int]:
    """Give a copied config its own port instead of the primary's. Returns it.

    Only a top-level integer `port` on a JSON **object** is touched — nested
    objects are left alone, and a file that is not an object, has no `port`, or
    does not parse is returned unchanged with `None`. A broken runtime config
    must not break worktree setup; it is the app's business to complain about
    its own file, not ours to fail the lane over.

    `wt` is the worktree root itself (not `dst`'s parent) so the issue number
    seeding `worktree_port` is read from `wt.name` directly — correct whether
    `dst` sits under `config/` or right at the worktree root.
    """
    raw = _load_json_object(dst)
    port_value = raw.get("port") if raw is not None else None
    # `bool` is an `int` subclass — exclude it explicitly, a `true` is not a port.
    if not isinstance(port_value, int) or isinstance(port_value, bool):
        return None
    port = _allocate_port(wt.name.rpartition(WT_SEP)[2], taken, wt, dst.name)
    raw["port"] = port
    runtime_files.save_text(dst, json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return port


_MACHINE_BOUND_TOKEN = "{onedrive}"
_WIN_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_machine_bound(value: str) -> bool:
    """True if a string value plausibly points at a real, non-worktree path.

    Two shapes cover every case seen so far: a template
    placeholder a repo's own config layer resolves against a real synced
    folder (`{onedrive}/...`), and a plain Windows absolute path (a drive
    letter into the primary checkout, another repo's data dir, or a synced
    folder written out in full). Deliberately narrow -- e.g. it does not
    flag URLs or POSIX-shaped strings that are actually route paths -- since
    a false positive here blanks a value a repo's own gate may need; a repo
    that needs finer control declares `blank_config_keys` explicitly instead
    of relying on this default.
    """
    if not value:
        return False
    if _MACHINE_BOUND_TOKEN in value.lower():
        return True
    return bool(_WIN_ABS_PATH_RE.match(value))


def _resolve_dotted_key(raw: dict, dotted: str) -> Optional[tuple]:
    """`(parent_dict, last_key)` for a dotted key present in `raw`, else `None`.

    A declared-but-absent key (or an intermediate that isn't an object) must
    not break setup -- same contract as `worktree_junction_targets`'s
    declared-but-absent path.
    """
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return None
    node = raw
    for part in parts[:-1]:
        if not (isinstance(node, dict) and part in node):
            return None
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return None
    return node, parts[-1]


def _blank_declared_keys(raw: dict, keys: list) -> list:
    """Blank exactly the dotted keys a repo declared, skipping any that are
    absent (see `_resolve_dotted_key`)."""
    blanked = []
    for dotted in keys:
        found = _resolve_dotted_key(raw, dotted)
        if found is None:
            continue
        node, last = found
        value = node[last]
        if isinstance(value, str) and value:
            node[last] = ""
            blanked.append(dotted)
        elif isinstance(value, list) and value:
            node[last] = []
            blanked.append(dotted)
    return blanked


def _is_read_safe(path: str, read_safe: "frozenset") -> bool:
    """True if `path` is a declared read-safe key, or sits under one.

    A declared `mirror` covers `mirror.dir` the same way `secret_config_keys`'
    `credentials` removes a whole sub-table -- one spelling of "this subtree",
    consistent between the two. Still fail-closed: only a **declared** subtree
    is exempt, so an undeclared sibling key is blanked as before.
    """
    if not read_safe:
        return False
    if path in read_safe:
        return True
    return any(path.startswith(f"{key}.") for key in read_safe)


def _blank_default_heuristic(node, prefix: str = "",
                             read_safe: "frozenset" = frozenset()) -> list:
    """Blank every machine-bound-looking string leaf, recursively.

    The conservative fallback for a repo that declares no
    `blank_config_keys` -- see `_looks_machine_bound`. List entries are
    filtered rather than blanked in place (a `folder_roots`-shaped list of
    paths just loses the dangerous entries); nested dicts/lists are walked.

    `read_safe` is the repo's declared read-only keys (see
    `worktree_read_safe_config_keys`), skipped so a side instance can actually
    reach the data it only reads. Exemption is opt-in and by exact key or
    declared subtree, so the default for anything undeclared is still to blank
    it -- an unknown path is unsafe.
    """
    blanked = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if _is_read_safe(path, read_safe):
                continue
            if isinstance(value, str):
                if _looks_machine_bound(value):
                    node[key] = ""
                    blanked.append(path)
            elif isinstance(value, list):
                filtered = [v for v in value if not (isinstance(v, str) and _looks_machine_bound(v))]
                if len(filtered) != len(value):
                    node[key] = filtered
                    blanked.append(path)
                for item in value:
                    if isinstance(item, (dict, list)):
                        blanked.extend(_blank_default_heuristic(item, path, read_safe))
            elif isinstance(value, dict):
                blanked.extend(_blank_default_heuristic(value, path, read_safe))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                blanked.extend(_blank_default_heuristic(item, prefix, read_safe))
    return blanked


def _worktree_key_list(repo: Path, key: str) -> Optional[list]:
    """A `[worktree] <key>` string list from a repo's `.fleet.toml`.

    `None` when nothing usable is declared (no `.fleet.toml`, no `[worktree]`
    table, no such key, a non-list value, or an unparseable file); otherwise
    the stripped non-empty strings, possibly `[]`. Same
    silent-degrade-on-any-error contract as `worktree_junction_targets`.
    """
    table = fleet_toml.table(fleet_toml.load(repo), "worktree")
    if table is None or key not in table:
        return None
    keys = table.get(key)
    if not isinstance(keys, list):
        return None
    return [k.strip() for k in keys if isinstance(k, str) and k.strip()]


def worktree_blank_config_keys(repo: Path) -> Optional[list]:
    """Dotted config keys a repo declares as machine-bound, from `.fleet.toml`:

        [worktree]
        blank_config_keys = ["mirror.dir", "mirror.backup_dir"]

    Returns `None` when nothing is declared (no `.fleet.toml`, no
    `[worktree]` table, no `blank_config_keys` key, or a malformed value) --
    the caller's signal to fall back to the conservative default heuristic.
    Returns a (possibly empty) list when the repo explicitly declares one; an
    empty list is a deliberate opt-out of the default heuristic entirely.
    """
    return _worktree_key_list(repo, "blank_config_keys")


def worktree_secret_config_keys(repo: Path) -> list:
    """Dotted config keys a repo declares must never reach a worktree copy:

        [worktree]
        secret_config_keys = ["auth.token", "credentials"]

    Additive and opt-in: unlike `blank_config_keys` this
    never selects a blanking mode, so undeclared, malformed and empty all mean
    the same thing -- remove nothing -- and the result is always a list.
    """
    return _worktree_key_list(repo, "secret_config_keys") or []


def worktree_read_safe_config_keys(repo: Path) -> list:
    """Dotted config keys a repo declares as read-only, from `.fleet.toml`:

        [worktree]
        read_safe_config_keys = ["projects_dir", "apps_scan_root"]

    Values under these keys survive the default blanking heuristic, because a
    side instance that only *reads* them needs them to boot usefully, and
    blanking them made standing an instance up a judgement call.
    Additive and opt-in like `secret_config_keys`: it never
    selects a blanking mode, so undeclared, malformed and empty all mean the
    same thing -- exempt nothing -- and the result is always a list.

    Fail-closed by construction. A key the running app *writes* must simply not
    be declared here, and an undeclared key -- including one added to the
    config after this list was written -- keeps being blanked. The dangerous
    direction needs an explicit, reviewable line in the repo's own
    `.fleet.toml`; the safe direction is the default.
    """
    return _worktree_key_list(repo, "read_safe_config_keys") or []


def worktree_primary_instance_ok(repo: Path) -> bool:
    """True if a repo declares that running out of its primary checkout is fine:

        [worktree]
        primary_instance_ok = true

    The repo-owned half of the side-instance refusal (`worktree_claim`'s
    `side_instance_state`). Some repos' operational lanes
    genuinely must run in the primary -- `life-os`, whose gitignored identity,
    context and state exist only in the primary clone -- and until now that
    exception was expressed solely as a `WORKTREE_CLAIM_ALLOW_PRIMARY=1`
    environment variable set by hand, i.e. as knowledge living in whichever
    session happened to remember it. Declaring it puts the exception in the
    repo that owns it, where it is reviewable.

    Anything other than a literal `true` -- absent, malformed, a string, a
    missing `.fleet.toml` -- is `False`. The refusal is the default.
    """
    table = fleet_toml.table(fleet_toml.load(repo), "worktree")
    return table is not None and table.get("primary_instance_ok") is True


def blank_machine_bound_config(dst: Path, declared_keys: Optional[list],
                               read_safe_keys: Optional[list] = None) -> list:
    """Blank machine-bound path values in a just-copied worktree config.

    `declared_keys` is `worktree_blank_config_keys(repo)`'s result -- `None`
    routes through the default heuristic, a list (including empty) blanks
    exactly those dotted keys. `read_safe_keys` is
    `worktree_read_safe_config_keys(repo)`'s result and exempts those keys from
    the **heuristic only**: a repo that declares `blank_config_keys` has named
    its blanking set exactly, and an explicit entry there wins, because the
    dangerous mistake is failing to blank. Same fail-open contract as
    `_repoint_config_port`: a file that isn't a JSON object, or doesn't
    parse, is left untouched rather than breaking setup. Returns the dotted
    keys actually blanked, for the caller to report.
    """
    raw = _load_json_object(dst)
    if raw is None:
        return []
    if declared_keys is not None:
        blanked = _blank_declared_keys(raw, declared_keys)
    else:
        blanked = _blank_default_heuristic(raw, read_safe=frozenset(read_safe_keys or ()))
    if blanked:
        runtime_files.save_text(dst, json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return blanked


def remove_secret_config(dst: Path, secret_keys: list) -> list:
    """Delete repo-declared secret keys from a just-copied worktree config.

    `secret_keys` is `worktree_secret_config_keys(repo)`'s result. Each dotted
    key present is removed outright, whatever its value -- a scalar, a list,
    or a whole sub-table (`"credentials"` drops the object, `"auth.token"`
    one leaf). Removed rather than blanked, so no value of the declared key --
    not even an empty placeholder of its type -- is reproduced in the copy,
    and one rule covers every value shape. Runs independently of
    `blank_machine_bound_config`, so it composes with either blanking mode.
    Same fail-open contract; absent keys are skipped. Returns the dotted keys
    actually removed.
    """
    if not secret_keys:
        return []
    raw = _load_json_object(dst)
    if raw is None:
        return []
    removed = []
    for dotted in secret_keys:
        found = _resolve_dotted_key(raw, dotted)
        if found is None:
            continue
        node, last = found
        del node[last]
        removed.append(dotted)
    if removed:
        runtime_files.save_text(dst, json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return removed


def _provision_copied_config(dst: Path, wt: Path, assigned: "set",
                             declared_keys: Optional[list], secret_keys: list,
                             read_safe_keys: Optional[list] = None) -> None:
    """The post-copy rewrites every copied config gets, in order: port
    repoint, secret-key removal, machine-bound blanking (removal first, so a
    declared secret is never also reported as blanked) -- each reported on
    stderr by count and dotted key name, never by value. Keys a repo declared
    read-safe are reported too, so a lane can see what was *kept* as well as
    what went, rather than discovering it by booting a broken instance."""
    port = _repoint_config_port(dst, wt, assigned)
    if port is not None:
        assigned.add(port)
        # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
        print(f"WORKTREE_PORT={port} ({dst.name})", file=sys.stderr)
    removed = remove_secret_config(dst, secret_keys)
    if removed:
        # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
        print(f"WORKTREE_CONFIG_SECRETS_REMOVED={len(removed)}: "
              f"{', '.join(removed)} ({dst.name})", file=sys.stderr)
    blanked = blank_machine_bound_config(dst, declared_keys, read_safe_keys)
    if blanked:
        # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
        print(f"WORKTREE_CONFIG_BLANKED={len(blanked)}: "
              f"{', '.join(blanked)} ({dst.name})", file=sys.stderr)
    if read_safe_keys:
        # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
        print(f"WORKTREE_CONFIG_READ_SAFE={len(read_safe_keys)}: "
              f"{', '.join(read_safe_keys)} ({dst.name})", file=sys.stderr)


def _config_dir_candidates(repo: Path) -> list:
    """`config/*.json` under `repo`, minus `*.sample.json` templates, sorted.

    The one candidate-discovery rule `copy_runtime_config` and
    `provisioned_configs` both need -- kept as a single
    function so a lane preflighting an already-provisioned worktree can never
    see a different candidate set than the one a real `copy_runtime_config`
    call would have copied.
    """
    src_dir = repo / "config"
    if not src_dir.is_dir():
        return []
    return sorted(p for p in src_dir.glob("*.json") if not p.name.endswith(".sample.json"))


def _root_config_candidates(repo: Path) -> list:
    """Root-level `*.json` under `repo` that git considers ignored, sorted.

    `copy_root_config`'s and `provisioned_configs`'s shared discovery rule,
    same reasoning as `_config_dir_candidates`.
    """
    candidates = sorted(p.name for p in repo.glob("*.json") if not p.name.endswith(".sample.json"))
    return [repo / name for name in sorted(_git_check_ignore(repo, candidates))]


def provisioned_configs(checkout: Path) -> list:
    """The gitignored runtime configs a worktree setup would have provisioned
    in `checkout` -- `config/*.json` minus templates, plus root-level
    `*.json` git considers ignored. Shares its candidate discovery with
    `copy_runtime_config` / `copy_root_config` (`_config_dir_candidates` /
    `_root_config_candidates`) so there is exactly one place that decides
    which files a worktree's runtime config actually is; a preflight and a
    real setup could otherwise silently drift apart on what counts.
    Called on the checkout itself (not the primary): the
    candidates it finds are already destination paths.
    """
    return _config_dir_candidates(checkout) + _root_config_candidates(checkout)


def _unsafe_config_keys(raw: object, declared_keys: Optional[list],
                        read_safe_keys: list) -> list:
    """Dotted keys in a provisioned config that still hold a machine-bound value.

    The check that makes a repeat of that incident impossible:
    a lane that hand-restored a path to make its instance boot has no way to
    know whether that path is read or written, and restoring a written one
    points the test instance at the owner's live state. Anything the
    provisioning pass would have emptied, and that is not *declared*
    read-safe, is reported unsafe. Fail-closed: an undeclared key is unsafe,
    never assumed fine.

    Which "provisioning pass" is the repo's own choice, and the two modes are
    asked the same question in their own terms. A repo that declares
    `blank_config_keys` has named its blanking set exactly, so only those keys
    are checked and the heuristic is not second-guessed; a repo that declares
    nothing gets the heuristic, minus its declared read-safe keys.

    `raw` is the already-parsed config. Both blanking passes mutate what they
    are given and report what they actually emptied -- on an already-blank copy
    they report nothing, which is the pass -- so this must be handed a
    throwaway parse, never a structure the caller goes on to use.
    """
    if not isinstance(raw, dict):
        return []
    if declared_keys is not None:
        return _blank_declared_keys(raw, declared_keys)
    return _blank_default_heuristic(raw, read_safe=frozenset(read_safe_keys))


def check_provisioned_configs(checkout: Path, declared_keys: Optional[list],
                              read_safe_keys: list) -> Tuple[List[str], List[str]]:
    """Re-verify every already-provisioned config in `checkout` is still safe
    to boot: no undeclared machine-bound value survived a hand-restore
    (`_unsafe_config_keys`), and no other live worktree already holds the same
    port. `(failures, unknowns)`; also **reserves** each config's port for
    `checkout` as a side effect (see the `side-instance-preflight` docstring in
    `worktree_claim.py` for why observing alone is not enough). Every
    `CONFIG_UNSAFE=`/`PORT=` line this prints is the one the CLI has always
    printed; only the discovery and the check itself moved here, next to
    `provisioned_configs` and the blanking helpers they share.
    """
    failures: List[str] = []
    unknowns: List[str] = []
    held = str(checkout.resolve())
    reservations = port_reservations()
    for dst in provisioned_configs(checkout):
        rel = dst.relative_to(checkout).as_posix()
        try:
            raw = json.loads(dst.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Same fail-open read the provisioning pass uses: a config that
            # isn't ours to parse is the app's business to complain about.
            continue
        # Read the port first: `_unsafe_config_keys` blanks the structure it is
        # given in order to learn what would have been blanked.
        port = raw.get("port") if isinstance(raw, dict) else None
        unsafe = _unsafe_config_keys(raw, declared_keys, read_safe_keys)
        if unsafe:
            failures.append(f"{rel} holds machine-bound values for undeclared keys: "
                            f"{', '.join(unsafe)}")
            # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
            print(f"CONFIG_UNSAFE={rel}: {', '.join(unsafe)}")
        if not isinstance(port, int) or isinstance(port, bool):
            continue
        # Claim it rather than merely observing it. A config provisioned before
        # reservations existed holds an unreserved port, and reporting that as
        # `unknown` forever would make the check observational -- it would name
        # the risk it is here to remove. Reserving on the spot makes the answer
        # total: either this checkout now holds the port exclusively, or
        # somebody else does and that is a real collision.
        row = reservations.get(port)
        if row is not None and row.get("worktree") != held:
            # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
            print(f"PORT={port} config={rel} reservation=HELD BY {row.get('worktree')}")
            failures.append(f"{rel}'s port {port} is reserved by "
                            f"{row.get('worktree')} — two instances would collide")
            continue
        try:
            holder = reserve_port(port, checkout, rel)
        except (OSError, TimeoutError) as exc:
            # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
            print(f"PORT={port} config={rel} reservation=unavailable "
                  f"({exc.__class__.__name__})")
            unknowns.append(f"{rel}'s port {port} could not be reserved, so "
                            f"exclusivity is not established")
            continue
        if holder is not None:
            # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
            print(f"PORT={port} config={rel} reservation=HELD BY {holder}")
            failures.append(f"{rel}'s port {port} is reserved by {holder} — "
                            f"two instances would collide")
            continue
        claimed = "" if row is not None else "  (claimed now)"
        listening = "" if _port_is_free(port) else "  (already listening)"
        # logging-not-print-exempt: KEY=value line that the calling skill parses from this stream
        print(f"PORT={port} config={rel} reservation=this checkout{claimed}{listening}")
    return failures, unknowns


def copy_runtime_config(repo: Path, wt: Path, assigned: Optional["set"] = None) -> list:
    """Copy the primary's gitignored `config/*.json` into a fresh worktree.

    `git worktree add` populates tracked files only, so a repo's own
    gitignored runtime config (`config/webapp_config.json`, `config/apps.json`,
    ...) never lands in the new worktree. An e2e suite that boots a disposable
    webapp+session-host then hits its own missing-config guard for nearly
    every test and mass-skips silently, while the pre-ship gate still reports
    green. `*.sample.json` templates are already tracked
    and excluded; a destination file that already exists (e.g. a prior partial
    setup) is left alone rather than overwritten. No-op if the repo has no
    `config/` dir. Returns the list of copied destination paths.

    The copy is byte-verbatim **except for a top-level `port`**, which is
    repointed into the `8500-8999` band, and any
    machine-bound path values, which are blanked (see
    `blank_machine_bound_config`). Carrying the primary's port across is what
    made every worktree lane's e2e suite report a collision with the user's
    live tray and refuse to run — a false positive, since the suite boots its
    own disposable instance on a free port and never touches the tray's. It
    also left a worktree that actually boots the app trying to bind the
    primary's port. Every other field -- secrets included, unless the repo
    declares them in `[worktree] secret_config_keys`, which are removed
    (see `remove_secret_config`) -- copies across untouched:
    the worktree must stay a faithful runtime twin, differing only where
    sharing (a live port, a real synced folder, a declared secret) is itself
    the bug.

    `assigned` collects ports handed out in this call; pass in a set shared
    with a sibling call (e.g. `copy_root_config` on the same worktree) so two
    ported configs in different locations can't compute the same port.
    """
    copied = []
    candidates = _config_dir_candidates(repo)
    if not candidates:
        return copied
    dst_dir = wt / "config"
    assigned = set() if assigned is None else assigned
    declared_keys = worktree_blank_config_keys(repo)
    secret_keys = worktree_secret_config_keys(repo)
    read_safe_keys = worktree_read_safe_config_keys(repo)
    for src in candidates:
        dst = dst_dir / src.name
        if dst.exists():
            continue
        runtime_files.make_dirs(dst_dir, exist_ok=True)
        runtime_files.copy_with_metadata(src, dst)
        copied.append(dst)
        _provision_copied_config(dst, wt, assigned, declared_keys, secret_keys,
                                 read_safe_keys)
    return copied


def copy_env_file(repo: Path, wt: Path) -> Optional[Path]:
    """Copy the primary's gitignored `.env` into a fresh worktree.

    `git worktree add` populates tracked files only, so a repo's own
    `.env` never lands in the new worktree, exactly like `config/*.json`
    (see `copy_runtime_config`). A hub or webapp that reads secrets/config
    from `.env` at startup (e.g. `local-llm-hub`'s `LOCAL_LLM_HUB_SSH_KEY`)
    then stalls on its own missing-value path until an unrelated timeout
    fires, and the e2e stage of a pre-ship gate goes red for a reason that
    has nothing to do with the diff. No-op, never a setup
    failure, if the primary has no `.env`. A destination file that already
    exists (e.g. a prior partial setup) is left alone rather than
    overwritten, same as `copy_runtime_config`. Returns the copied
    destination path, or None if there was nothing to copy.
    """
    src = repo / ".env"
    if not src.is_file():
        return None
    dst = wt / ".env"
    if dst.exists():
        return None
    runtime_files.copy_with_metadata(src, dst)
    return dst


def _git_check_ignore(repo: Path, names: list) -> "set":
    """Names among `names` (repo-root-relative) that git considers ignored.

    `git check-ignore` exits 1 when nothing matches -- not an error, so this
    runs with `check=False` and trusts only stdout, one matched name per line.
    """
    if not names:
        return set()
    res = _git(repo, "check-ignore", "--", *names, check=False)
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


def copy_root_config(repo: Path, wt: Path, assigned: Optional["set"] = None) -> list:
    """Copy the primary's gitignored root-level `*.json` into a fresh worktree.

    Some repos keep their runtime config at the repo root instead of under
    `config/` (`accounting-quarterly`'s `config.json`, `home-automation`'s
    `devices.json`) -- `copy_runtime_config` doesn't reach those, so a
    worktree for one of these repos was missing its config entirely.
    Which root-level `*.json` files matter is asked of
    git itself (`git check-ignore`) rather than hardcoded, so a repo that
    adds a new one is covered with no code change here; a tracked
    `*.sample.json` template is excluded up front as belt-and-braces, though
    a tracked file is never gitignored in the first place. Same rules as
    `copy_runtime_config`: an existing destination is left alone, a
    top-level `port` is repointed via `_repoint_config_port`,
    machine-bound values are blanked via `blank_machine_bound_config`, and
    declared secret keys are removed via `remove_secret_config`.
    Returns the list of copied destination paths.

    `assigned` collects ports handed out in this call; `setup_worktree`
    passes the same set it gave `copy_runtime_config` so a `config/*.json`
    file and a root-level file can't be repointed to the same port.
    """
    copied = []
    candidates = _root_config_candidates(repo)
    if not candidates:
        return copied
    assigned = set() if assigned is None else assigned
    declared_keys = worktree_blank_config_keys(repo)
    secret_keys = worktree_secret_config_keys(repo)
    read_safe_keys = worktree_read_safe_config_keys(repo)
    for src in candidates:
        dst = wt / src.name
        if dst.exists():
            continue
        runtime_files.copy_with_metadata(src, dst)
        copied.append(dst)
        _provision_copied_config(dst, wt, assigned, declared_keys, secret_keys,
                                 read_safe_keys)
    return copied
