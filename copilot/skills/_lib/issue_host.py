"""Remote-URL parsing, the managed-issue marker and the gh/glab issue argv shapes.

The audit and learning-log helpers all drive `gh` (GitHub) or `glab` (GitLab)
from the same `origin` remote. This module owns what they share: which host a
remote is, the `owner/repo` inside it, the `audit-managed` marker that makes an
issue findable by identity, and the CLI argv for list / create / edit / comment.
It spawns nothing and writes nothing: each caller keeps its own subprocess
spawn, so error handling, dry-run and test stubs stay with the caller.

The GitLab shapes follow `glab`'s documented CLI and are not live-tested, so a
fix to one of them lands here once instead of in three scripts.
"""

from __future__ import annotations

import re
from typing import Optional

_MARKER_RE = re.compile(r"^[ \t]*<!--[ \t]*audit-managed:[ \t]*kind=([\w-]+)[ \t]*-->[ \t]*$", re.MULTILINE)


# ---- remote URL -------------------------------------------------------------

def parse_owner_repo(remote_url: str) -> str:
    """'owner/repo' (GitLab subgroups keep their slashes) or '' if unparseable."""
    u = re.sub(r"\.git$", "", (remote_url or "").strip())
    if not u:
        return ""
    if "://" in u:
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+/(.+)$", u)
        path = m.group(1) if m else ""
    else:
        path = u.partition(":")[2]
    parts = [p for p in path.strip("/").split("/") if p]
    return "/".join(parts) if len(parts) >= 2 else ""


def split_owner_repo(full: str) -> tuple[str, str]:
    """('group/subgroup', 'project') from 'group/subgroup/project'; ('', '') when empty."""
    owner, _, repo = (full or "").rpartition("/")
    return (owner, repo) if owner and repo else ("", "")


def detect_host(remote_url: str) -> str:
    """'github' for github.com, else 'gitlab' (gitlab.com or self-hosted)."""
    return "github" if "github.com" in (remote_url or "") else "gitlab"


def cli_for(host: str) -> str:
    return "gh" if host == "github" else "glab"


# ---- managed-issue marker ---------------------------------------------------

def marker_for(kind: str) -> str:
    return f"<!-- audit-managed: kind={kind} -->"


def has_marker(body: str, kind: str) -> bool:
    """True only when the marker is the body's first line, so a body that merely
    quotes the marker in prose (an issue documenting this format) is not managed."""
    m = _MARKER_RE.match((body or "").lstrip())
    return bool(m) and m.group(1) == kind


def ensure_marker(body: str, kind: str) -> str:
    stripped = _MARKER_RE.sub("", body or "").lstrip("\n")
    return f"{marker_for(kind)}\n\n{stripped}" if stripped else f"{marker_for(kind)}\n"


# ---- gh / glab argv ---------------------------------------------------------

def list_issues_args(host: str, repo: str, label: str, state: str = "all") -> list[str]:
    """`state` is 'all' or 'open'. Always label-filtered: an unfiltered list is
    capped by the CLI and silently misses the ledger on a repo with hundreds of issues."""
    if host == "github":
        return ["issue", "list", "--repo", repo, "--state", state, "--label", label,
                "--limit", "300", "--json", "number,title,body,state,url"]
    # glab lists open issues unless asked for all.
    return ["issue", "list", "--repo", repo, *(["--all"] if state == "all" else []),
            "--label", label, "--per-page", "100", "--output", "json"]


def normalise_issues(host: str, data: object) -> list[dict]:
    """Parsed `issue list` JSON as {number, title, body, state, url}; state is 'open' or 'closed'."""
    items = data if isinstance(data, list) else []
    if host == "github":
        return [{**i, "state": (i.get("state") or "").lower()} for i in items]
    return [{"number": i.get("iid"), "title": i.get("title", ""), "body": i.get("description") or "",
             "state": "open" if i.get("state") == "opened" else "closed", "url": i.get("web_url")}
            for i in items]


def view_url_args(host: str, repo: str, number: int) -> list[str]:
    if host == "github":
        return ["issue", "view", str(number), "--repo", repo, "--json", "url"]
    return ["issue", "view", str(number), "--repo", repo, "--output", "json"]


def url_from_view(host: str, data: object) -> Optional[str]:
    """The issue URL out of parsed `issue view` JSON, or None."""
    if not isinstance(data, dict):
        return None
    return data.get("url") if host == "github" else data.get("web_url")


def label_create_args(host: str, repo: str, label: str,
                      color: Optional[str] = None, description: Optional[str] = None) -> list[str]:
    """`color` is bare hex ('0e8a16'); glab wants the leading '#'."""
    args = (["label", "create", label, "--repo", repo] if host == "github"
            else ["label", "create", "--repo", repo, "-n", label])
    if color:
        args += ["--color", color if host == "github" else f"#{color}"]
    if description:
        args += ["--description", description]
    return args


def create_issue_args(host: str, repo: str, title: str, body_file: str, label: str) -> list[str]:
    if host == "github":
        return ["issue", "create", "--repo", repo, "--title", title, "--body-file", body_file,
                "--label", label, "--assignee", "@me"]
    return ["issue", "create", "--repo", repo, "--title", title, "--description-file", body_file,
            "--label", label, "--assignee", "@me", "--yes"]


def edit_issue_args(host: str, repo: str, number: int, title: str, body_file: str, label: str) -> list[str]:
    if host == "github":
        return ["issue", "edit", str(number), "--repo", repo, "--title", title, "--body-file", body_file,
                "--add-label", label]
    return ["issue", "update", str(number), "--repo", repo, "--title", title,
            "--description-file", body_file, "--label", label]


def comment_args(host: str, repo: str, number: int, body_file: str) -> list[str]:
    if host == "github":
        return ["issue", "comment", str(number), "--repo", repo, "--body-file", body_file]
    return ["issue", "note", str(number), "--repo", repo, "--message-file", body_file]


def created_issue_url(out: str) -> str:
    """The URL a create call prints: its last line ('' when it printed nothing)."""
    return out.splitlines()[-1] if out else ""
