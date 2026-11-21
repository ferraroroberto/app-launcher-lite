"""`chief_ops.py merge` and `mr-create`: the lite merge path, and a refused merge as a Board question.

A lite lane ships through a GitLab merge request. This verb asks GitLab to merge
the request from `--branch`, squashed and with the source branch removed, as
soon as its pipeline passes (`glab mr merge --auto-merge`; a project with no
pipeline merges at once). It then reads the request back, so the verdict is
GitLab's state and not the command's exit code:

  MERGE=merged   GitLab shows the request merged.
  MERGE=queued   auto-merge is set; GitLab merges it when the pipeline passes.
  MERGE=refused  GitLab refused (a failed pipeline, a conflict, a missing
                 approval, a closed request) and the `reason=` says why.
  MERGE=unknown  glab is missing, signed out, or its answer could not be read.

With `--ask`, a refusal also becomes a question on the chief's plan (`plan ask`,
id `merge-<repo>-<N>`), so it reaches Roberto's Board instead of dying in a
lane's terminal. The chief and chief-managed lanes pass `--ask`. A repeat
refusal for the same item reuses the open question.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import chief_plan

from . import journal, launcher, rails

Glab = Callable[[List[str]], subprocess.CompletedProcess]

# Opening a request is one API call (the branch is already pushed), so a minute
# is generous; past it glab is waiting on something no one will answer.
MR_CREATE_TIMEOUT = 60.0
_MR_URL = re.compile(r"https?://\S+/-/merge_requests/\d+")


_BANNERS = frozenset({"ERROR", "WARNING", "WARN", "INFO"})
_NEUTRAL_STATUSES = frozenset({"", "mergeable", "checking", "unchecked"})


class Unknown(Exception):
    """glab could not answer."""


def first_message(text: str) -> str:
    """The first useful line of glab's output.

    glab boxes a failure under a banner line that is only the level word
    (`ERROR`), so the first non-empty line carries no information.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if line and line.upper() not in _BANNERS:
            return line
    return ""


def _glab(glab: Glab, args: List[str]) -> subprocess.CompletedProcess:
    try:
        return glab(args)
    except FileNotFoundError as exc:
        raise Unknown("glab not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise Unknown("glab timed out") from exc


def mr_state(project: str, branch: str, glab: Glab) -> Dict[str, Any]:
    """The merge request from `branch` as glab shows it (`state`, `iid`, `web_url`)."""
    res = _glab(glab, ["mr", "view", branch, "--repo", project, "--output", "json"])
    if res.returncode != 0:
        raise Unknown(f"glab mr view failed: {first_message(res.stderr or res.stdout)[:200] or 'no detail'}")
    try:
        data = json.loads(res.stdout or "")
    except ValueError as exc:
        raise Unknown("unparseable glab mr view output") from exc
    if not isinstance(data, dict):
        raise Unknown("glab mr view output was not an object")
    return data


def merge(project: str, branch: str, glab: Glab) -> Tuple[str, Dict[str, Any], str]:
    """`(verdict, merge request, reason)` for one merge attempt."""
    res = _glab(glab, ["mr", "merge", branch, "--repo", project, "--auto-merge", "--squash",
                       "--remove-source-branch", "--yes"])
    refusal = first_message(res.stderr or res.stdout)
    mr = mr_state(project, branch, glab)
    state = str(mr.get("state") or "")
    if state == "merged":
        return "merged", mr, ""
    if res.returncode != 0:
        # glab's text can be a bare lead-in ("All attempts fail:" over a 405), so the request's own
        # merge status (conflict, ci_must_pass, ...) says what GitLab is actually waiting on.
        status = str(mr.get("detailed_merge_status") or "")
        said = refusal.rstrip(":") or f"glab mr merge exited {res.returncode}"
        named = status in _NEUTRAL_STATUSES or status in said
        return "refused", mr, said if named else f"{said} (merge status: {status})"
    if state == "opened":
        return "queued", mr, ""
    return "refused", mr, f"merge request is {state or 'in an unknown state'}"


def question_id(repo: str, number: int) -> str:
    return f"merge-{repo}-{number}"


def ask_on_refusal(plan_path: Path, repo: str, number: int, iid: Any, reason: str) -> str:
    """File (or reuse) the plan question for a refused merge; returns its id."""
    ref = f"{repo}#{number}"
    qid = question_id(repo, number)
    question = f"GitLab refused to merge {ref} (merge request !{iid or '?'}): {reason}"

    def mutate(doc: Dict[str, Any]) -> None:
        if any(item.get("id") == qid for item in doc.get("waiting_on_roberto") or []):
            return
        chief_plan.add_question(
            doc, question, ref=ref, item_id=qid,
            detail="The lane's merge request did not merge. GitLab's reason is quoted above.",
            recommendation="Fix in the lane: the refusal usually names something the lane can repair.",
            options=["Fix in the lane::send the lane the refusal to repair and push",
                     "Retry::merge again once the cause is gone",
                     "Leave it::park the item with the reason"],
            recommended=["Fix in the lane"],
        )

    chief_plan.update(plan_path, mutate)
    journal.log_transition(journal.journal_for_plan(plan_path), "asked", f"asked: {question}", ref)
    return qid


def cmd_merge(args: argparse.Namespace, glab: Glab = launcher.run_glab) -> int:
    repo, number = rails.parse_issue_ref(args.ref)
    owner, why = (None, "") if args.project else launcher.resolve_owner(repo, args.owner)
    if owner is None and not args.project:
        print(f"MERGE=unknown reason={launcher.OWNER_UNKNOWN} ({why})")
        return 1
    project = args.project or f"{owner}/{repo}"
    try:
        verdict, mr, reason = merge(project, args.branch, glab)
    except Unknown as exc:
        print(f"MERGE=unknown reason={exc}")
        return 1
    tail = f" mr=!{mr.get('iid')}" + (f" url={mr['web_url']}" if mr.get("web_url") else "")
    print(f"MERGE={verdict}{tail}" + (f" reason={reason}" if reason else ""))
    if verdict == "refused" and args.ask:
        plan_path = Path(args.plan_file) if args.plan_file else chief_plan.plan_file()
        print(f"MERGE_ASKED={ask_on_refusal(plan_path, repo, number, mr.get('iid'), reason)}")
    return 0 if verdict in ("merged", "queued") else 1


def error_detail(text: str) -> str:
    """The message under glab's last `ERROR` banner as one line, else `first_message`.

    `mr create` prints a progress line and a recovery hint before the banner, so
    its first line says nothing about why the request was not created.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    banners = [i for i, line in enumerate(lines) if line.upper() == "ERROR"]
    if banners:
        detail = " ".join(line for line in lines[banners[-1] + 1:] if line)
        if detail:
            return detail[:300]
    return first_message(text)


def create_mr(number: int, glab: Glab) -> Tuple[str, str]:
    """`(verdict, detail)` for opening the current branch's merge request.

    `--yes` skips glab's submission prompt, and `--push=false` leaves the push to
    the caller's own `git push`: glab would otherwise spawn git, which the
    timeout cannot kill. `launcher.run_glab` closes stdin, so any prompt that
    remains fails at once instead of waiting.
    """
    try:
        res = glab(["mr", "create", "--fill", "--yes", "--push=false", "--description", f"Closes #{number}",
                    "--assignee", "@me", "--remove-source-branch", "--squash-before-merge"])
    except FileNotFoundError:
        return "unknown", "glab not installed"
    except subprocess.TimeoutExpired as exc:
        return "timeout", f"glab mr create gave no answer within {exc.timeout:g}s and was killed"
    found = _MR_URL.search(f"{res.stdout or ''}\n{res.stderr or ''}")
    if res.returncode == 0 and found:
        return "created", found.group(0)
    return "failed", error_detail(res.stderr or res.stdout) or f"glab mr create exited {res.returncode}"


def cmd_mr_create(args: argparse.Namespace, glab: Optional[Glab] = None) -> int:
    run = glab or functools.partial(launcher.run_glab, timeout=args.timeout)
    verdict, detail = create_mr(args.number, run)
    print(f"MR={verdict} {'url' if verdict == 'created' else 'reason'}={detail}")
    return 0 if verdict == "created" else 1


def add_parser(sub: argparse._SubParsersAction) -> None:
    m = sub.add_parser("merge", help="merge a lane's GitLab merge request when its pipeline passes")
    m.add_argument("ref", help="<repo>#<N> the merge request closes")
    m.add_argument("--branch", required=True, help="the merge request's source branch")
    m.add_argument("--ask", action="store_true", help="on a refusal, add a Board question to the chief's plan")
    m.add_argument("--owner", default=None, help="GitLab namespace (default: the launcher's gitlab_group)")
    m.add_argument("--project", default=None, help="full GitLab path; overrides <owner>/<repo>")
    m.add_argument("--plan-file", default=None)
    m.set_defaults(func=cmd_merge)

    c = sub.add_parser("mr-create", help="open the current branch's merge request with no prompt, bounded by a timeout")
    c.add_argument("number", type=int, help="the issue number the merge request closes")
    c.add_argument("--timeout", type=float, default=MR_CREATE_TIMEOUT, help="seconds before glab is killed")
    c.set_defaults(func=cmd_mr_create)
