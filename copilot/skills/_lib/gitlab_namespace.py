"""The GitLab namespace and host every lite forge read is addressed to.

app-launcher-lite owns the namespace: its `gitlab_group` setting, served by
`GET /api/config` on loopback. When that is empty or unreachable, the checkout's own
`origin` remote names it. Neither source answers -> `(None, reason)`, so a caller prints
`OWNER=unknown` instead of asking GitLab about a namespace nobody configured (a
guessed one reads as a 404, which looks like "no such issue").

The host works the same way: `gitlab_host` from the launcher, else the host of the
checkout's `origin` when that is not GitHub, else gitlab.com. It reaches glab as
`GITLAB_HOST`, because glab sends a bare `group/project` to its default host. The token
stays in glab's own config: nothing here reads, stores or logs one.
"""

from __future__ import annotations

import http.client
import os
import subprocess
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import issue_host
import lite_ports
import lite_webapp
from git_run import run_git

# app-launcher-lite's webapp, on the port its own config.json sets; `lite_webapp`
# swaps in the scheme that port serves.
DEFAULT_BASE_URL = lite_ports.webapp_url()
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CONFIG_TIMEOUT = 5.0
GIT_TIMEOUT = 15.0
DEFAULT_HOST = "gitlab.com"


def _launcher_config(base_url: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """`(config, "")` from the launcher's `GET /api/config`, else `(None, why)`."""
    base_url = base_url or DEFAULT_BASE_URL
    host = urllib.parse.urlsplit(base_url).hostname
    if host is None or host.lower() not in _LOOPBACK_HOSTS:
        return None, f"refusing a non-loopback launcher host {host!r}"
    try:
        config = lite_webapp.request(base_url, "/api/config", timeout=CONFIG_TIMEOUT)
    except urllib.error.HTTPError as exc:
        exc.close()
        return None, f"launcher config unreadable (HTTP {exc.code})"
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return None, f"launcher config unreachable ({type(exc).__name__})"
    return (config, "") if isinstance(config, dict) else (None, "launcher config was not an object")


def from_config(base_url: Optional[str] = None) -> Tuple[Optional[str], str]:
    """`(group, "")` from the launcher's `gitlab_group`, else `(None, why)`."""
    config, why = _launcher_config(base_url)
    if config is None:
        return None, why
    group = str(config.get("gitlab_group") or "").strip().strip("/")
    return (group, "") if group else (None, "launcher gitlab_group is empty")


def _origin_url(repo_path: Path) -> str:
    try:
        res = run_git(["-C", str(repo_path), "remote", "get-url", "origin"], timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout if res.returncode == 0 else ""


def from_checkout(repo_path: Path) -> Optional[str]:
    """The namespace in `repo_path`'s `origin` remote (subgroups kept), or None."""
    return issue_host.split_owner_repo(issue_host.parse_owner_repo(_origin_url(repo_path)))[0] or None


def host_from_config(base_url: Optional[str] = None) -> str:
    """The launcher's `gitlab_host` as typed (trailing slash dropped), or "" when unset or unreadable."""
    config, _why = _launcher_config(base_url)
    return str(config.get("gitlab_host") or "").strip().rstrip("/") if config else ""


def host_from_remote(remote_url: str) -> str:
    """The GitLab host `remote_url` names, or "" for no usable remote or a GitHub one."""
    if issue_host.detect_host(remote_url) == "github":
        return ""
    return issue_host.parse_host(remote_url)


def resolve_host(repo_path: Optional[Path] = None, base_url: Optional[str] = None,
                 remote_url: Optional[str] = None) -> str:
    """The GitLab host glab is pointed at: `gitlab_host`, else the remote's host, else gitlab.com.

    `remote_url` is a remote the caller already holds; otherwise `repo_path`'s `origin` is read.
    """
    configured = host_from_config(base_url)
    if configured:
        return configured
    if remote_url is None and repo_path is not None:
        remote_url = _origin_url(repo_path)
    return host_from_remote(remote_url or "") or DEFAULT_HOST


def glab_env(host: str) -> Dict[str, str]:
    """The child environment that points glab at `host`; glab's own config supplies the token."""
    return {**os.environ, "GITLAB_HOST": host or DEFAULT_HOST}


def tried(host: str, project: str = "") -> str:
    """` (tried host <host>, project <project>)`, appended to a glab failure so a wrong host or group reads off one line."""
    where = f"host {host or DEFAULT_HOST}" + (f", project {project}" if project else "")
    return f" (tried {where})"


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
