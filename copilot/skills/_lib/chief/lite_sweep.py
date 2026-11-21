"""`chief_ops.py sweep`: finished lane worktrees, judged by the lite forge.

fleet-config's `cmd_sweep` with two seams changed. A worktree is removable once a
merged pull/merge request covers its HEAD; the lookup now dispatches on the
remote's host, since a lite repo may live on GitLab or still on GitHub: GitLab
goes through `glab mr list` (normalised to the `gh pr list` rows `chief_sweep`
reads), GitHub through the ported `chief_sweep.github_pr_lookup`. The fleet-wide
"stale primaries" report is dropped: it reads the full fleet's vendored-wave
state, which lite does not carry. The scan, the verdicts and `--apply` are the
verbatim `chief/chief_sweep.py`.

Scope: the sweep folder is the parent of this checkout, which on a machine that
also runs the full fleet holds that fleet's worktrees too. Lite only ever touches
worktrees of lite repos: a repo named in lite's own state (the chief-managed
registry and the active-issue markers under `~/.copilot/hooks/state`) or named
`*-lite`. Every other worktree is reported as out of scope and never removed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import active_issue
import chief_managed
import issue_host
import worktree_claim
from worktree_residue import WT_SEP

from . import chief_sweep, launcher

Glab = Callable[[List[str]], subprocess.CompletedProcess]


def normalise_mrs(rows: Any) -> Optional[List[Dict[str, Any]]]:
    """`glab mr list --output json` rows as `{number, state, headRefOid}`, or None if not a list."""
    if not isinstance(rows, list):
        return None
    return [{"number": r.get("iid"), "state": str(r.get("state") or "").upper(), "headRefOid": r.get("sha") or ""}
            for r in rows if isinstance(r, dict)]


def gitlab_mr_lookup(remote_url: str, branch: str, glab: Glab = launcher.run_glab) -> Optional[List[Dict[str, Any]]]:
    """Every merge request (any state) from `branch`, or None if not established."""
    project = issue_host.parse_owner_repo(remote_url)
    if not project:
        return None
    try:
        res = glab(["mr", "list", "--repo", project, "--source-branch", branch, "--all", "--output", "json"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    try:
        return normalise_mrs(json.loads(res.stdout or ""))
    except ValueError:
        return None


def forge_lookup(remote_url: str, branch: str) -> Optional[List[Dict[str, Any]]]:
    """The request lookup for the remote's host; None for a host neither CLI serves."""
    if issue_host.detect_host(remote_url) == "github":
        return chief_sweep.github_pr_lookup(remote_url, branch)
    return gitlab_mr_lookup(remote_url, branch)


LITE_SUFFIX = "-lite"


def lite_repos(*row_sets: Dict[str, Any]) -> Set[str]:
    """Repo names lite's own state names (rows carrying `repo`)."""
    return {str(row["repo"]) for rows in row_sets for row in rows.values()
            if isinstance(row, dict) and row.get("repo")}


def in_lite_scope(worktree_name: str, named: Set[str]) -> bool:
    primary = worktree_name.split(WT_SEP, 1)[0]
    return primary in named or primary.endswith(LITE_SUFFIX)


def split_scope(findings: Iterable[chief_sweep.Finding], named: Set[str]) -> Tuple[list, list]:
    """`(lite findings, out-of-scope worktree names)`."""
    kept, skipped = [], []
    for finding in findings:
        (kept if in_lite_scope(finding.name, named) else skipped).append(finding)
    return kept, [f.name for f in skipped]


def cmd_sweep(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else chief_sweep.default_root()
    cards: Optional[List[Dict[str, Any]]] = None
    try:
        cards = chief_sweep.board_cards(launcher._request(args.base_url, "/api/board"))
    except (OSError, ValueError) as exc:
        # An unreadable board is an unestablished fact, not an empty one: every
        # removable verdict degrades to `unknown`.
        print(f"NOTE: board unreadable ({exc}); removable verdicts are unknown", file=sys.stderr)
    managed = active_issue.read_rows(chief_managed.state_file())
    markers = active_issue.read_rows()
    findings = chief_sweep.scan(
        root,
        dirs=None if cards is None else chief_sweep.live_dirs(cards),
        markers=markers,
        session_ids=None if cards is None else chief_sweep.live_session_ids(cards),
        pr_lookup=forge_lookup,
        managed=managed,
    )
    findings, outside = split_scope(findings, lite_repos(managed, markers))
    stuck = 0
    if args.apply:
        stuck = chief_sweep.apply_findings(findings, lambda wt: worktree_claim.remove_worktree(wt))
    sessions = chief_sweep.stale_sessions(cards or [], managed, launcher.fetch_issue_state)
    print("\n".join(chief_sweep.format_report(findings, sessions, applied=args.apply)))
    if outside:
        print(f"OUT_OF_SCOPE={len(outside)} (not lite repos, never touched): {', '.join(sorted(outside))}")
    return 1 if stuck else 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    sw = sub.add_parser("sweep", help="classify (and with --apply remove) finished lane worktrees")
    chief_sweep.add_cli(sw)
    sw.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    sw.set_defaults(func=cmd_sweep)
