"""The GitLab namespace every lite forge read is addressed to.

app-launcher-lite owns it: its `gitlab_group` setting, served by `GET /api/config`
on loopback. When that is empty or unreachable, the checkout's own `origin` remote
names it. Neither source answers -> `(None, reason)`, so a caller prints
`OWNER=unknown` instead of asking GitLab about a namespace nobody configured (a
guessed one reads as a 404, which looks like "no such issue").
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

import issue_host
import lite_ports
from git_run import run_git

# app-launcher-lite's webapp, on the port its own config.json sets.
DEFAULT_BASE_URL = lite_ports.webapp_url()
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CONFIG_TIMEOUT = 5.0
GIT_TIMEOUT = 15.0


def from_config(base_url: Optional[str] = None) -> Tuple[Optional[str], str]:
    """`(group, "")` from the launcher's `gitlab_group`, else `(None, why)`."""
    base_url = base_url or DEFAULT_BASE_URL
    host = urllib.parse.urlsplit(base_url).hostname
    if host is None or host.lower() not in _LOOPBACK_HOSTS:
        return None, f"refusing a non-loopback launcher host {host!r}"
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/config", timeout=CONFIG_TIMEOUT) as resp:
            config = json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        exc.close()
        return None, f"launcher config unreadable (HTTP {exc.code})"
    except (OSError, ValueError) as exc:
        return None, f"launcher config unreachable ({type(exc).__name__})"
    group = str(config.get("gitlab_group") or "").strip().strip("/") if isinstance(config, dict) else ""
    return (group, "") if group else (None, "launcher gitlab_group is empty")


def from_checkout(repo_path: Path) -> Optional[str]:
    """The namespace in `repo_path`'s `origin` remote (subgroups kept), or None."""
    try:
        res = run_git(["-C", str(repo_path), "remote", "get-url", "origin"], timeout=GIT_TIMEOUT)
    except OSError:
        return None
    if res.returncode != 0:
        return None
    return issue_host.split_owner_repo(issue_host.parse_owner_repo(res.stdout))[0] or None


def resolve(repo_path: Optional[Path] = None, base_url: Optional[str] = None) -> Tuple[Optional[str], str]:
    """`(namespace, "")`, or `(None, why)` when neither the launcher nor the checkout names one."""
    group, reason = from_config(base_url)
    if group:
        return group, ""
    if repo_path is not None:
        owner = from_checkout(repo_path)
        if owner:
            return owner, ""
        reason += f"; no origin remote namespace in {repo_path.name}"
    return None, reason
