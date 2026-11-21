"""Is a fleet repo's declared long-running service listening right now, and at
which commit?

The capability sits apart from the worktree-claim FSM because `land-primary`'s
fourth guard is only one of its callers: fast-forwarding a checkout a
tray/webapp is *serving* leaves static assets on disk ahead of the Python
holding them in memory, so the claim lifecycle had to ask whether anything was
up before landing a merge. Nothing about the question is worktree-specific --
`/issue-finish`, `restart_and_verify_webapp`, and any future health sweep want
the same answer -- and reaching it meant importing the 1400-line claim module.
So it lives here, and `worktree_claim` imports it.

Three concerns, in dependency order:

  - the **vocabulary**: `SERVICE_LIVE` / `SERVICE_ABSENT` / `SERVICE_UNKNOWN`,
    three states rather than a boolean, because "nothing is listening" is a
    positive finding and "the probe could not be completed" is the absence of
    one (the fleet rule that an unestablished fact reports as its own state);
  - the **pure verdict**, `live_service_check`, which turns a declaration plus
    a probe result into `(ok, reason)` with no I/O in it at all -- unit-tested
    exhaustively in `tests/worktree_claim/test_claim_and_landing.py`;
  - the **impure probe**: `declared_service` (read the declaration out of
    `hooks/projects.toml`), `listening_ports` (the OS's own TCP listener
    table), `_running_sha` (best-effort `git_sha` off the declared version
    endpoint), and `probe_service` / `service_state` tying them together.

stdlib only, and no import of `worktree_claim` -- the dependency runs one way.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleet_core import fleet_repo_scan  # noqa: E402
from no_window import NO_WINDOW  # noqa: E402

# ---- live-service vocabulary for the fourth land-primary guard -----
#
# What a liveness probe established, kept as three states rather than a
# boolean: "nothing is listening" is a *positive* finding, while "the probe
# could not be completed" is the absence of one, and the fleet rule is that an
# unestablished fact reports as its own state instead of folding into the
# passing one.
SERVICE_LIVE = "live"        # the declared port is in the OS listener table
SERVICE_ABSENT = "absent"    # readable table, port not in it -- nothing is serving
SERVICE_UNKNOWN = "unknown"  # unreadable table, or no port declared to probe


def live_service_check(
    declared: bool,
    probe: str,
    *,
    port: Optional[int] = None,
    running_sha: Optional[str] = None,
    detail: str = "",
) -> Tuple[bool, str]:
    """Decide whether a long-running process forbids fast-forwarding this tree.

    Pure half of the fourth `land-primary` guard. The first three guards
    ask three questions -- is the claim mine, is the tree clean, is it on its
    default branch -- and all three were satisfied when an app-launcher
    lane fast-forwarded a primary whose tray was *serving* it, leaving two
    static files on disk ahead of the Python holding them in memory.

    The asymmetry decides the default. **Not** landing leaves disk and process
    both at the old commit: behind, but coherent. **Landing** leaves disk new
    and process old: behind *and* skewed, serving one UI out of two commits.
    Both need the same restart, so refusing costs nothing the restart won't
    fix, and there is deliberately no "landed anyway" success variant.

    `declared` is the repo's `hooks/projects.toml` `tray_cmd` declaration --
    the fleet already distinguishes exactly the right set, and `fleet-config`
    declaring none is correct: nothing runs out of it and landing it is what
    makes a merge live through the `~/.claude` junctions. `probe` is what the
    live check established (`SERVICE_LIVE` / `SERVICE_ABSENT` /
    `SERVICE_UNKNOWN`); a probe that could not be completed is **not** proof of
    absence, so it refuses. Every refusal names the restart as the remedy, so
    an operator reads it as work parked rather than a tool that broke.
    """
    where = f"webapp :{port}" if port else "tray declared, no webapp_port to probe"
    if running_sha:
        where += f" at {running_sha}"
    if not declared:
        return True, f"no long-running service declared{f' ({detail})' if detail else ''}"
    if probe == SERVICE_ABSENT:
        return True, f"declared service not running ({detail or where})"
    if probe == SERVICE_LIVE:
        return False, (f"live process serving this tree ({where}); "
                       f"restart required, not a fast-forward")
    return False, (f"cannot confirm the declared service is stopped ({detail or where}); "
                   f"refusing rather than assuming idle -- restart required if it is up")


# ---- live-service probe (impure half of the land-primary guard) -------------------

# A refusal costs a restart the operator already owes; a slow probe costs every
# finish on the fleet. Both budgets are deliberately small (netstat measures at
# ~0.03s on this host).
SERVICE_PROBE_TIMEOUT = 5.0
SERVICE_HTTP_TIMEOUT = 1.5


def declared_service(
    repo: Path, projects_toml: Optional[Path] = None
) -> Tuple[bool, Optional[int], Optional[str], str]:
    """Read `repo`'s service declaration from `hooks/projects.toml`.

    Returns `(declares_tray, webapp_port, api_version_path, detail)`. The
    fleet's existing membership table already separates exactly the right set:
    a `tray_cmd` is the declaration that a long-running
    process serves that tree, so no new configuration file is introduced --
    adding one would be the wrong move.

    Three states, not two (one `False` for the last two would treat a repo
    that was never entered in the table like one that declares no tray on purpose):

    - **registered, with a `tray_cmd`**: `True`, with the port/api path to
      probe.
    - **registered, no `tray_cmd`**: a *known* absence -- `fleet-config`
      itself is this case, and it must keep landing or every merge here
      stops deploying through the `~/.claude` junctions. Returns `False`;
      `detail` names the repo.
    - **absent from the table entirely, same as an unreadable table**:
      nothing was established -- unlike the case above, this is not evidence
      the repo runs no service, only that nothing says so. Returns `True`
      with no port, which routes `service_state` to `SERVICE_UNKNOWN` and
      refuses; `detail` names the missing declaration as the remedy.
    """
    try:
        tables = fleet_repo_scan.fleet_repo_tables(projects_toml)
    except (OSError, ValueError) as exc:
        return True, None, None, f"could not read hooks/projects.toml: {exc}"
    target = os.path.normcase(os.path.normpath(str(repo)))
    for name, tbl in tables.items():
        prefix = os.path.normcase(os.path.normpath(str(tbl.get("cwd_prefix", ""))))
        if prefix != target:
            continue
        if not tbl.get("tray_cmd"):
            return False, None, None, f"{name} declares no tray_cmd"
        port = tbl.get("webapp_port")
        return True, int(port) if isinstance(port, int) else None, tbl.get("api_version_path"), ""
    return (True, None, None,
            f"{repo.name} is not declared in hooks/projects.toml -- "
            "declare it there to land")


def listening_ports() -> Optional[set]:
    """Every TCP port something is listening on, or None if that can't be read.

    The OS's own listener table (`netstat -ano -p tcp`), because a connect
    attempt cannot answer the question on this fleet: measured on this host, a
    connect to a *closed* loopback port does not come back refused -- it hangs
    until the timeout and raises `TimeoutError`, indistinguishable from a hung
    service. Classifying that as "absent" would land the tree anyway; the
    listener table is positive evidence in both directions.

    Two parsing rules, deliberately not the state word alone: a row is a
    listener when its foreign address is the all-zero wildcard, or when the
    state column literally reads LISTENING. `netstat` localises that word on a
    non-English Windows, and a parse that silently matched nothing would report
    every port idle -- the exact failure this guard exists to prevent. An empty
    result is therefore treated as a *failed read* (None), never as "no
    listeners": no live Windows box has zero.

    Decoded as `oem`, never `text=True`: this process sets PYTHONUTF8, and a
    console tool's OEM output decoded as UTF-8 comes back empty rather than
    raising (global CLAUDE.md).
    """
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True,
            encoding="oem", errors="replace",
            timeout=SERVICE_PROBE_TIMEOUT, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    ports = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].upper().startswith("TCP"):
            continue
        if parts[2] not in ("0.0.0.0:0", "[::]:0") and parts[3].upper() != "LISTENING":
            continue
        tail = parts[1].rsplit(":", 1)[-1]
        if tail.isdigit():
            ports.add(int(tail))
    return ports or None


def probe_service(port: Optional[int], api_path: Optional[str]) -> Tuple[str, Optional[str], str]:
    """Ask the machine whether the declared service is actually up right now.

    Returns `(probe, running_sha, detail)`. A port in the listener table is a
    live process serving the tree (`SERVICE_LIVE`); a readable table without it
    is positive evidence that nothing is (`SERVICE_ABSENT` -- landing is then
    safe). No port to probe, or a table that could not be read, establishes
    nothing (`SERVICE_UNKNOWN`), and an unestablished fact is never folded into
    the passing state. Where an `api_version_path` is declared the running
    build's `git_sha` is fetched too -- best-effort decoration for the refusal
    message, never part of the verdict: something listening is already the
    whole answer.
    """
    if not port:
        return SERVICE_UNKNOWN, None, "no webapp_port declared to probe"
    listening = listening_ports()
    if listening is None:
        return SERVICE_UNKNOWN, None, f"could not read the TCP listener table to check :{port}"
    if port not in listening:
        return SERVICE_ABSENT, None, f"nothing listening on :{port}"
    return SERVICE_LIVE, running_sha(port, api_path), ""


_INSECURE_CTX = ssl._create_unverified_context()  # self-signed certs are normal in our fleet


def running_sha(port: int, api_path: Optional[str]) -> Optional[str]:
    """The `git_sha` the live service reports, or None. Never raises.

    Most fleet apps serve HTTPS with a self-signed cert; a few (local-llm-hub)
    serve plain HTTP -- so HTTPS first, then HTTP. Decoration only: a failure
    here costs the refusal message its commit, never its verdict. Public because
    `design_review.ledger` records it as `live_build`, the
    one place a run's target commit and the served build are kept apart.
    """
    if not api_path:
        return None
    for scheme in ("https", "http"):
        url = f"{scheme}://127.0.0.1:{port}{api_path}"
        ctx = _INSECURE_CTX if scheme == "https" else None
        try:
            with urllib.request.urlopen(url, timeout=SERVICE_HTTP_TIMEOUT, context=ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, ConnectionError, OSError, ssl.SSLError):
            continue
        sha = payload.get("git_sha") if isinstance(payload, dict) else None
        if sha:
            return str(sha)
    return None


def service_state(repo: Path, projects_toml: Optional[Path] = None) -> Tuple[bool, str]:
    """`live_service_check`'s verdict for `repo`: declaration + live probe."""
    declared, port, api_path, detail = declared_service(repo, projects_toml)
    if not declared:
        return live_service_check(False, SERVICE_UNKNOWN, detail=detail)
    probe, sha, probe_detail = probe_service(port, api_path)
    # The declaration's own detail wins: the only way it is non-empty here is
    # an unreadable projects.toml, and that is the more useful thing to say
    # than the probe's downstream "no webapp_port to probe".
    return live_service_check(True, probe, port=port, running_sha=sha,
                              detail=detail or probe_detail)

