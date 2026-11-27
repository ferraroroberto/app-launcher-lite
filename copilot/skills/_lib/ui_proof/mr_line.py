"""The `VISUAL_PROOF=<state>` line in a GitLab merge request description.

The Board's GitLab client reads the first `VISUAL_PROOF=` in the description of
the merged request that closed an issue, so the line is kept at the top, where
no mention further down can stand in for it, and there is only ever one.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import gitlab_namespace

from chief import launcher, lite_merge

Glab = Callable[[List[str]], subprocess.CompletedProcess]

_OWN_LINE = re.compile(r"^VISUAL_PROOF=\S*[ \t]*\r?(?:\n|\Z)(?:[ \t]*\r?\n)?", re.MULTILINE)


def with_line(description: str, state: str) -> str:
    """`description` with exactly one `VISUAL_PROOF=<state>` line, at the top; everything else kept."""
    rest = _OWN_LINE.sub("", description).lstrip("\r\n")
    return f"VISUAL_PROOF={state}\n" + (f"\n{rest}" if rest else "")


def _description(run: Glab, mr: int) -> Tuple[Optional[str], str]:
    """`(description, "")`, or `(None, why)` when glab did not show one."""
    res = run(["mr", "view", str(mr), "--output", "json"])
    if res.returncode != 0:
        return None, lite_merge.first_message(res.stderr or res.stdout)[:200] or "glab mr view failed"
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        return None, "unparseable glab mr view output"
    # A reply without the key is not an empty description: writing over it would drop `Closes #N`.
    if not isinstance(data, dict) or "description" not in data:
        return None, "glab mr view showed no description"
    return str(data["description"] or "").replace("\r\n", "\n"), ""


def _update(run: Glab, mr: int, description: str) -> subprocess.CompletedProcess:
    # A file, not an argument: `--description -` opens an editor and a long one passes the command-line limit.
    with tempfile.TemporaryDirectory(prefix="ui-proof-mr-") as scratch:
        body = Path(scratch) / "description.md"
        body.write_bytes(description.encode("utf-8"))  # bytes: no newline translation on Windows
        return run(["mr", "update", str(mr), "--description-file", str(body)])


def set_line(repo: Path, mr: int, state: str, glab: Optional[Glab] = None) -> Tuple[bool, str]:
    """Set the line on merge request `mr` of `repo`'s GitLab project; `(ok, MR_LINE=... line)`.

    `written` is only said after a read back shows it. `unknown` means glab took
    the update and the read back could not confirm it.
    """
    # No `--repo`: glab reads the project from the checkout's remote, as `mr create` does.
    run = glab or functools.partial(launcher.run_glab, host=gitlab_namespace.resolve_host(repo), cwd=repo)
    try:
        current, why = _description(run, mr)
        if current is None:
            return False, f"MR_LINE=failed reason={why}"
        wanted = with_line(current, state)
        if wanted.strip() == current.strip():
            return True, "MR_LINE=unchanged"
        res = _update(run, mr, wanted)
        if res.returncode != 0:
            detail = lite_merge.first_message(res.stderr or res.stdout)[:200] or "glab mr update failed"
            return False, f"MR_LINE=failed reason={detail}"
        after, why = _description(run, mr)
    except FileNotFoundError:
        return False, "MR_LINE=failed reason=glab not installed"
    except subprocess.TimeoutExpired:
        return False, "MR_LINE=unknown reason=glab gave no answer and was killed"
    if after is None or not after.startswith(f"VISUAL_PROOF={state}"):
        return False, f"MR_LINE=unknown reason={why or 'the read back does not show the line'}"
    return True, "MR_LINE=written"
