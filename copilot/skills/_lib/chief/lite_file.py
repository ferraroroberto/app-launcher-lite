"""The lite `chief_ops.py file` verb: file one GitLab issue through the launcher.

The chief never runs `glab` writes itself, so this is its one filing path: the
same `POST /api/board/issues/file` the Board's file button uses. The route takes
`{repo, title, body}` and no labels, and answers `{repo, url}`; this verb reads
the issue number back out of that url and prints it as `FILED=<repo>#<N>`.
"""

from __future__ import annotations

import argparse
import re
import urllib.error
from pathlib import Path
from typing import Optional

from . import launcher

# glab runs server-side before the launcher answers, so this outlasts the default budget.
FILE_TIMEOUT = 60.0

# GitLab issue urls end `/issues/<iid>`, `/-/issues/<iid>` or, on current GitLab, `/-/work_items/<iid>`.
_ISSUE_NUMBER = re.compile(r"/(?:issues|work_items)/(\d+)/?(?:[?#].*)?$")


def issue_number(url: object) -> Optional[int]:
    """The issue number a filed-issue url ends with, or None when it carries none."""
    match = _ISSUE_NUMBER.search(str(url or "").strip())
    return int(match.group(1)) if match else None


def _http_detail(exc: urllib.error.HTTPError) -> str:
    detail = exc.read().decode("utf-8", "replace").strip()[:300]
    return detail or str(exc.reason)


def _unconfirmed(repo: str, why: str) -> int:
    # glab may have created the issue before the launcher failed to answer, so a
    # blind retry can file a duplicate: the caller re-reads the board first.
    print(f"UNCONFIRMED={why}; the issue may exist on {repo}. Run `chief_ops.py board` and look "
          f"for it before filing again: a retry can file a duplicate.")
    return 1


def cmd_file(args: argparse.Namespace) -> int:
    title = args.title.strip()
    if not title:
        print("REFUSED=title is empty")
        return 1
    try:
        body = Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"REFUSED=body file unreadable: {args.body_file} ({exc.strerror or exc})")
        return 1
    if not body.strip():
        print(f"REFUSED=body file is empty: {args.body_file}")
        return 1
    try:
        result = launcher._request(args.base_url, "/api/board/issues/file", method="POST",
                                   body={"repo": args.repo, "title": title, "body": body},
                                   timeout=FILE_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            return _unconfirmed(args.repo, f"the launcher answered HTTP {exc.code}: {_http_detail(exc)}")
        print(f"REFUSED=the launcher refused the filing (HTTP {exc.code}): {_http_detail(exc)}")
        return 1
    except (TimeoutError, urllib.error.URLError) as exc:
        if not launcher.is_timeout(exc):
            raise  # refused/unreachable: nothing was sent, `main` prints UNREACHABLE=
        return _unconfirmed(args.repo, f"no answer within {FILE_TIMEOUT:g}s")
    repo = str(result.get("repo") or args.repo)
    number = issue_number(result.get("url"))
    if number is None:
        return _unconfirmed(repo, "the launcher's reply carried no issue number")
    print(f"FILED={repo}#{number}")
    print(f"URL={result['url']}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    f = sub.add_parser("file", help="file a GitLab issue through the launcher")
    f.add_argument("repo", help="a project name in the projects folder")
    f.add_argument("--title", required=True)
    f.add_argument("--body-file", required=True, help="the issue body, as a UTF-8 file")
    f.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    f.set_defaults(func=cmd_file)
