"""Can `git` reach GitLab with no prompt? The check behind install.ps1's checklist line.

`glab auth login` keeps its token in the keyring for glab's own API calls; `git`
asks its own credential helper, which on a fresh PC holds no GitLab credential.
glab "signed in" therefore proves nothing about `git fetch`/`git push`, and a lane
(which runs with prompts off) stops on the first fetch. This asks git itself: one
`git ls-remote` against a configured GitLab https remote with prompts disabled.

Three states, never folded together: `ok` (git answered), `missing` (git was refused
for want of a credential, with the fix) and `unknown` (no https GitLab remote to ask,
or the answer was not an auth verdict: network, timeout, git absent). The fix is only
printed; this tool never writes git config.

CLI:  python gitlab_git_access.py check [--repo PATH] [--remote URL]
prints `GIT_ACCESS=ok|missing|unknown`, `REMOTE=`, `HOST=`, `REASON=` and `FIX=` lines,
and always exits 0 so a checklist can render it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import issue_host  # noqa: E402
from git_run import run_git  # noqa: E402

OK, MISSING, UNKNOWN = "ok", "missing", "unknown"
PROBE_TIMEOUT_S = 20.0
# Bounds the sibling scan: a projects folder holds tens of checkouts, never thousands.
MAX_SIBLINGS = 200
# GCM_INTERACTIVE=never stops Git Credential Manager opening a login window, which a
# prompt-less lane would otherwise hang on; the probe must fail fast instead.
_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
# Phrases git, GitLab and GCM print when the refusal is for want of a credential.
_AUTH_MARKERS = (
    "terminal prompts disabled", "could not read username", "could not read password",
    "authentication failed", "http basic: access denied", "interactivity has been disabled",
)

Runner = Callable[..., subprocess.CompletedProcess]


class Verdict(NamedTuple):
    status: str
    remote: str
    host: str
    reason: str
    fix: List[str]


def _origin(path: Path, run: Runner) -> str:
    try:
        res = run(["-C", str(path), "config", "--get", "remote.origin.url"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _https_gitlab(url: str) -> bool:
    """True for an https remote that is not GitHub: the only kind a credential helper serves."""
    return url.lower().startswith("https://") and issue_host.detect_host(url) == "gitlab"


def redact(url: str) -> str:
    """`url` without any user-info, so a token embedded in a remote is never printed."""
    parts = urllib.parse.urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def find_remote(repo_path: Path, run: Runner = run_git) -> str:
    """The first https GitLab `origin` of this checkout, else of a sibling checkout; '' if none."""
    candidates = [repo_path]
    try:
        siblings = sorted(p for p in repo_path.parent.iterdir() if p.is_dir() and p != repo_path)
    except OSError:
        siblings = []
    candidates += [p for p in siblings[:MAX_SIBLINGS] if (p / ".git").exists()]
    for path in candidates:
        url = _origin(path, run)
        if _https_gitlab(url):
            return url
    return ""


def _glab_helper_wired(host: str, run: Runner) -> bool:
    """True when git config already routes this host's credentials to glab."""
    try:
        res = run(["config", "--get-all", f"credential.https://{host}.helper"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and "glab" in res.stdout


def fix_lines(host: str, wired: bool) -> List[str]:
    """What the owner runs: glab's helper scoped to `host`, or a fresh sign-in when it is wired."""
    if wired:
        return [f"glab auth login --hostname {host}   (git already uses glab for {host}; glab has no usable token)"]
    # The empty first value clears Git Credential Manager for this host only; `--%` keeps
    # Windows PowerShell 5.1 from dropping the empty argument.
    return [
        f'git --% config --global --add credential.https://{host}.helper ""',
        f'git config --global --add credential.https://{host}.helper "!glab auth git-credential"',
    ]


def classify(res: subprocess.CompletedProcess) -> tuple[str, str]:
    """`(status, reason)` for a finished `git ls-remote`."""
    if res.returncode == 0:
        return OK, "git answered without a prompt"
    err = (res.stderr or "").strip()
    first = next((ln.strip() for ln in err.splitlines() if ln.strip()), f"git exited {res.returncode}")
    if any(marker in err.lower() for marker in _AUTH_MARKERS):
        return MISSING, first
    return UNKNOWN, f"git failed, not an auth refusal: {first}"


def check(repo_path: Path, remote: str = "", run: Runner = run_git) -> Verdict:
    url = remote or find_remote(repo_path, run)
    if not url:
        return Verdict(UNKNOWN, "", "", "no https GitLab remote found in this checkout or its sibling checkouts", [])
    host = urllib.parse.urlsplit(url).netloc.rpartition("@")[2]
    shown = redact(url)
    try:
        res = run(["ls-remote", "--exit-code", url, "HEAD"], timeout=PROBE_TIMEOUT_S, extra_env=_NO_PROMPT_ENV)
    except subprocess.TimeoutExpired:
        return Verdict(UNKNOWN, shown, host, f"git did not answer within {PROBE_TIMEOUT_S:.0f}s", [])
    except OSError as exc:
        return Verdict(UNKNOWN, shown, host, f"git could not be run ({type(exc).__name__})", [])
    status, reason = classify(res)
    fix = fix_lines(host, _glab_helper_wired(host, run)) if status == MISSING else []
    return Verdict(status, shown, host, reason, fix)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--repo", default=".", help="checkout to start from (default: cwd)")
    parser.add_argument("--remote", default="", help="probe this https URL instead of finding one")
    args = parser.parse_args(argv)
    verdict = check(Path(args.repo).resolve(), args.remote)
    print(f"GIT_ACCESS={verdict.status}")
    print(f"REMOTE={verdict.remote}")
    print(f"HOST={verdict.host}")
    print(f"REASON={verdict.reason}")
    for line in verdict.fix:
        print(f"FIX={line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
