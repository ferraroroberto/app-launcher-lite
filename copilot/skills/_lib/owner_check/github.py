"""Every forge call the owner-check package makes, behind one seam (`_glab`): GitLab.

Lite: fleet-config's `owner_check/github.py` speaks `gh`. The lite fleet's forge
is GitLab, so this module keeps that module's name and every function the
verbatim `cli`, `backfill`, `reclassify` and `repair` call, and answers them
through `glab`. A call that fails raises `GhError` (the name the callers catch);
callers turn that into the `unknown` state, never into "nothing to do". Tests
replace `_glab` and never touch the network.

Labels: by default the owner-check labels are the plain `needs-owner` and
`needs-validation`, which work on any GitLab tier. A project that opts in with
`[owner_check] label_scope = "<scope>"` in this repo's `.fleet.toml` gets
GitLab scoped labels (`<scope>::needs-owner`, one of a kind per issue) instead.
Reads map a scoped label back to its plain name, so `block.LABELS` and every
reader that checks `"needs-owner" in labels` stay unchanged.

The fleet is every sibling checkout of this repo with a GitLab remote; `--repo`
names one explicitly. GitLab numbers issues and merge requests separately, so
`is_pull_request` is always False and a "PR" here is a merge request iid.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_LIB = Path(__file__).resolve().parent.parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import git_run  # noqa: E402
import gitlab_namespace  # noqa: E402
from no_window import NO_WINDOW  # noqa: E402

from owner_check import block  # noqa: E402

GITLAB_HOST = "gitlab.com"
GLAB_TIMEOUT_SECONDS = 60
# A fleet-wide list is ~2 calls per repo plus one notes read per labelled issue.
LIST_WORKERS = 6
REPO_ROOT = _LIB.parents[1]
SCOPE_SEP = "::"


class GhError(RuntimeError):
    """A forge call that did not establish its answer (the callers' name for it)."""


def _glab(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed executable, argv built here
            ["glab", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GLAB_TIMEOUT_SECONDS, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise GhError("glab not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"glab timed out after {GLAB_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        raise GhError((proc.stderr or proc.stdout or f"glab {args[0]} failed").strip())
    return proc.stdout


def _json(args: Sequence[str]) -> Any:
    out = _glab(args)
    try:
        return json.loads(out or "null")
    except ValueError as exc:
        raise GhError(f"unparseable glab {args[0]} output: {exc}") from exc


def namespace(repo: str) -> str:
    """The GitLab namespace `repo` lives in (the launcher's `gitlab_group`, else its checkout's remote).

    Unresolved raises `GhError` so every caller reports `unknown` rather than reading a guessed
    namespace's 404 as "no such issue".
    """
    owner, why = gitlab_namespace.resolve(REPO_ROOT.parent / repo)
    if owner is None:
        raise GhError(f"OWNER=unknown ({why})")
    return owner


def __getattr__(name: str) -> str:
    """`OWNER`, resolved on read: the byte-identical `cli.py` compares a qualified ref against it.

    Unresolved yields a value no real owner equals, so such a ref is refused, never accepted.
    """
    if name == "OWNER":
        return gitlab_namespace.resolve()[0] or "(owner unknown)"
    raise AttributeError(name)


def _slug(repo: str) -> str:
    return f"{namespace(repo)}/{repo}"


def _api(repo: str, path: str) -> str:
    return f"projects/{urllib.parse.quote(_slug(repo), safe='')}/{path}"


def label_scope(fleet_toml: Optional[Path] = None) -> str:
    """The opt-in scope from `.fleet.toml` `[owner_check] label_scope`, else "" (plain labels)."""
    try:
        data = tomllib.loads((fleet_toml or REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    scope = (data.get("owner_check") or {}).get("label_scope")
    return scope.strip() if isinstance(scope, str) else ""


def forge_label(label: str) -> str:
    """The label as written on GitLab: scoped when the scope is opted in."""
    scope = label_scope()
    return f"{scope}{SCOPE_SEP}{label}" if scope else label


def plain_label(name: str) -> str:
    """A GitLab label as the readers know it: an owner-check scoped label loses its scope."""
    _scope, sep, tail = name.rpartition(SCOPE_SEP)
    return tail if sep and tail in block.LABEL_STYLE else name


def _labels(raw: Any) -> List[str]:
    return [plain_label(str(name)) for name in raw or []]


def _notes(repo: str, number: int) -> List[str]:
    rows = _json(["api", _api(repo, f"issues/{number}/notes?sort=asc&per_page=100")])
    return [str(r.get("body") or "") for r in rows or [] if isinstance(r, dict) and not r.get("system")]


def fetch_issue(repo: str, number: int) -> Dict[str, Any]:
    """Title, body, state, labels (names), comment bodies oldest first, closing merge request urls."""
    data = _json(["issue", "view", str(number), "--repo", _slug(repo), "--output", "json"])
    if not isinstance(data, dict):
        raise GhError("glab issue view output was not an object")
    closers = _json(["api", _api(repo, f"issues/{number}/closed_by")])
    return {
        "repo": repo,
        "number": int(data.get("iid") or number),
        "title": data.get("title") or "",
        "body": data.get("description") or "",
        "state": {"opened": "OPEN", "closed": "CLOSED"}.get(str(data.get("state")), str(data.get("state") or "")),
        "url": data.get("web_url") or "",
        "labels": _labels(data.get("labels")),
        "comments": _notes(repo, number),
        "prs": [str(m.get("web_url")) for m in closers or [] if isinstance(m, dict) and m.get("web_url")],
    }


def _status(diff: Dict[str, Any]) -> str:
    if diff.get("new_file"):
        return "added"
    if diff.get("deleted_file"):
        return "removed"
    return "renamed" if diff.get("renamed_file") else "modified"


def pr_files(repo: str, pr: int) -> List[Tuple[str, str]]:
    """`(status, path)` per changed file of merge request `pr`, readable after merge."""
    diffs = _json(["api", "--paginate", _api(repo, f"merge_requests/{pr}/diffs?per_page=100")])
    return [(_status(d), str(d.get("new_path") or d.get("old_path") or ""))
            for d in diffs or [] if isinstance(d, dict)]


def pr_body(repo: str, pr: int) -> str:
    """The merge request's description, where `/issue-finish` records the `VISUAL_PROOF=` line."""
    data = _json(["api", _api(repo, f"merge_requests/{pr}")])
    return str((data or {}).get("description") or "") if isinstance(data, dict) else ""


def pr_url(repo: str, pr: int) -> str:
    return f"https://{GITLAB_HOST}/{_slug(repo)}/-/merge_requests/{pr}"


def ensure_label(repo: str, label: str) -> None:
    color, description = block.LABEL_STYLE[label]
    try:
        _glab(["label", "create", "--repo", _slug(repo), "--name", forge_label(label),
               "--color", f"#{color}", "--description", description])
    except GhError as exc:
        if "already" not in str(exc).lower():
            raise


def _edit_labels(repo: str, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
    args = ["issue", "update", str(number), "--repo", _slug(repo)]
    for label in add:
        args += ["--label", forge_label(label)]
    for label in remove:
        args += ["--unlabel", forge_label(label)]
    _glab(args)


def _post_comment(repo: str, number: int, body: str) -> None:
    _glab(["issue", "note", str(number), "--repo", _slug(repo), "--message", body])


def mark(repo: str, number: int, kind: str, body: str) -> str:
    """Create the kind's label if missing, post the block, then apply the label. Returns the label.

    The block goes first: a failure between the two calls leaves an unlabelled
    note (harmless, the Board reads labels) instead of a label with no note.
    """
    label = block.LABELS[kind]
    ensure_label(repo, label)
    _post_comment(repo, number, body)
    _edit_labels(repo, number, add=[label])
    return label


def note(repo: str, number: int, body: str) -> None:
    """Post a block on an issue that already carries its label (repair)."""
    _post_comment(repo, number, body)


def clear(repo: str, number: int, present_labels: Sequence[str], body: str) -> List[str]:
    """Remove every owner-check label the issue carries and post the cleared block."""
    removed = [label for label in block.LABELS.values() if label in present_labels]
    if removed:
        _edit_labels(repo, number, remove=removed)
    _post_comment(repo, number, body)
    return removed


def _rows(rows: Any) -> List[Dict[str, Any]]:
    """`glab issue list` rows in the `gh` shape the callers read."""
    return [{"number": r.get("iid"), "title": r.get("title") or "", "body": r.get("description") or "",
             "state": {"opened": "OPEN", "closed": "CLOSED"}.get(str(r.get("state")), str(r.get("state") or "")),
             "url": r.get("web_url") or "", "labels": _labels(r.get("labels")), "closedAt": r.get("closed_at")}
            for r in rows or [] if isinstance(r, dict)]


def closed_issues(repo: str) -> List[Dict[str, Any]]:
    """Recently closed issues with what the rule reads, for the back-fill dry run.

    GitLab records no "not planned" reason, so `stateReason` is absent and every
    closed issue reads as completed.
    """
    return _rows(_json(["issue", "list", "--repo", _slug(repo), "--closed", "--per-page", "100", "--output", "json"]))


def is_pull_request(repo: str, number: int) -> bool:
    """Always False: GitLab numbers issues and merge requests separately."""
    return False


def _labelled(repo: str, label: str) -> List[Dict[str, Any]]:
    rows = _rows(_json(["issue", "list", "--repo", _slug(repo), "--all", "--label", forge_label(label),
                        "--per-page", "100", "--output", "json"]))
    for row in rows:
        row["comments"] = [{"body": text} for text in _notes(repo, int(row["number"]))]
    return rows


def _item(repo: str, row: Dict[str, Any], kind: str) -> Dict[str, Any]:
    latest = block.latest_block(str(c.get("body") or "") for c in row.get("comments") or []) or {}
    return {
        "repo": repo,
        "number": row.get("number"),
        "ref": f"{repo}#{row.get('number')}",
        "title": row.get("title") or "",
        "url": row.get("url") or "",
        "state": row.get("state") or "",
        "kind": kind,
        # A label with no block is still an item; its fields stay empty, never invented.
        "block": bool(latest) and latest.get("kind") == kind,
        "reason": latest.get("reason", "") if latest.get("kind") == kind else "",
        "try": latest.get("try", "") if latest.get("kind") == kind else "",
        "proof": latest.get("proof", []) if latest.get("kind") == kind else [],
        "since": latest.get("since", "") if latest.get("kind") == kind else "",
    }


def fleet_repo_names(projects_toml: Optional[Path] = None) -> List[str]:
    """Sibling checkouts of this repo whose remotes point at GitLab, by folder name."""
    names = []
    for path in sorted(REPO_ROOT.parent.iterdir()):
        if not (path / ".git").is_dir():
            continue  # worktrees carry a .git file; their primary is listed already
        proc = git_run.run_git(["-C", str(path), "remote", "-v"], timeout=GLAB_TIMEOUT_SECONDS)
        if proc.returncode == 0 and GITLAB_HOST in (proc.stdout or ""):
            names.append(path.name)
    return names


def merge_same_try(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One Board line per flow: items of one repo and kind with an identical `try` merge."""
    kept: List[Dict[str, Any]] = []
    first: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for item in items:
        key = (item["repo"], item["kind"], str(item.get("try") or ""))
        if not key[2]:
            kept.append(item)
        elif key in first:
            first[key].setdefault("merged", []).append(item["ref"])
        else:
            first[key] = item = dict(item)
            kept.append(item)
    return kept


def same_try_siblings(repo: str, number: int, kind: str, try_text: str) -> List[int]:
    """Other issues in `repo` labelled for `kind` whose latest block carries the same `try`."""
    if not try_text:
        return []
    rows = _labelled(repo, block.LABELS[kind])
    return [int(row["number"]) for row in rows
            if row.get("number") != number and _item(repo, row, kind)["try"] == try_text]


def newest_first(items: List[Dict[str, Any]]) -> None:
    """Sort in place: latest `since` first, undated items last, ties by repo and number."""
    items.sort(key=lambda it: (it["repo"], it["number"] or 0))
    items.sort(key=lambda it: str(it.get("since") or ""), reverse=True)


def list_items(repos: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Every issue carrying an owner-check label, fleet-wide: the reader API.

    Returns `{"items": [...], "unknown": [{"repo", "reason"}]}`. A repo that
    could not be read lands in `unknown`, never silently in an empty list.
    """
    unknown: List[Dict[str, str]] = []
    if repos is None:
        try:
            repos = fleet_repo_names()
        except OSError as exc:
            return {"items": [], "unknown": [{"repo": "*", "reason": str(exc)}]}
    jobs = [(repo, kind, label) for repo in repos for kind, label in block.LABELS.items()]

    def run(job: Tuple[str, str, str]) -> Tuple[str, str, Any]:
        repo, kind, label = job
        try:
            return repo, kind, _labelled(repo, label)
        except (GhError, ValueError) as exc:
            return repo, kind, exc

    items: List[Dict[str, Any]] = []
    failed = set()
    with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
        for repo, kind, result in pool.map(run, jobs):
            if isinstance(result, Exception):
                if repo not in failed:
                    unknown.append({"repo": repo, "reason": str(result)})
                    failed.add(repo)
                continue
            items.extend(_item(repo, row, kind) for row in result)
    newest_first(items)
    return {"items": items, "unknown": unknown}
