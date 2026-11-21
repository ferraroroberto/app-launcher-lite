"""Gate evidence bound to a working-tree content hash.

Why this exists
----------------
`/issue-finish` skipped the remote CI wait on the phrase "green this session":
the model's recollection that a gate or e2e run passed earlier. Nothing tied
that pass to the content about to merge, and the finish flow itself edits the
tree after the gate (design-drift fix commits, e2e suite upkeep). This helper
runs the gate, binds its exit code to a hash of the whole working tree, and
answers later whether that result still describes the tree.

Subcommands
-----------
  run --label <L> [--repo <path>] -- <command argv...>
        Hashes the tree, runs the command itself (inheriting stdout/stderr,
        so the exit code is observed rather than reported), hashes again, and
        records ``{cmd, cmd_sha256, exit, wtree, utc}`` for ``L`` only when
        both hashes agree. A tree that changed mid-run, or a hash that could
        not be computed, drops any earlier record for ``L`` -- never leaves a
        stale pass behind. Exits with the command's own exit code; the last
        stdout line is ``EVIDENCE=recorded ...`` or ``EVIDENCE=not_recorded
        reason=...``.

  check --label <L> [--repo <path>] [--expect-cmd "<command>"]
        Prints exactly one of:
          FRESH     a recorded exit-0 run of ``L`` matches the current tree (exit 0)
          STALE     one exists, but the tree changed since (exit 1)
          MISSING   no recorded passing run, or ``--expect-cmd`` differs (exit 1)
          UNKNOWN   the tree hash could not be computed (exit 2)
        STALE names the changed paths on stderr.

  check-red [--label red] [--repo <path>]
        Did the recorded run of ``L`` FAIL on a tree other than the current one (a bug fix's test seen red
        before the fix)? Prints exactly one of RED (exit 0), NOT_RED (the record passed or
        never started), SAME_TREE, MISSING (exit 1) or UNKNOWN (exit 2).

Tree hash = the content of the whole working tree, untracked-but-not-ignored
files included, without touching the real index: copy the index (resolved via
``rev-parse --git-path index``, so a linked worktree works) to a temp file,
point ``GIT_INDEX_FILE`` at it, ``git add -A``, ``git write-tree``, delete the
copy (mtime kept, see the comment in ``tree_hash``). Committing identical content therefore stays FRESH; any edit or new
untracked file goes STALE. Evidence lives at ``rev-parse --git-path
gate-evidence.json``, outside the working tree (writing it must not change
the hash it records) and per worktree. Ignored files (``.env``, ``.venv``)
stay out because nothing here ever passes ``-f``.

stdlib only. Pure-ish logic is exercised end to end against temp repos in
``tests/e2e/test_gate_evidence.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from active_issue import pid_state  # noqa: E402
from fleet_core import (
    hooks_state,  # noqa: E402
    runtime_files,  # noqa: E402
)
from fleet_core.utf8_stdio import ensure_utf8_stdio  # noqa: E402
from git_run import run_git  # noqa: E402
from no_window import NO_WINDOW  # noqa: E402

EVIDENCE_NAME = "gate-evidence.json"
# A marker per running gate under the hooks-state dir, so a lane never compacts
# mid-gate. `hooks/lane_compact` reads the same folder (tree boundary: its own
# spelling, held together by the acceptance suite).
GATE_INFLIGHT_DIRNAME = "gate-inflight"

# `git add -A` over a huge untracked tree must not hang a finish: past this it
# reports UNKNOWN, which consumers treat like STALE (re-run / watch CI).
HASH_TIMEOUT_SECONDS = 120


def _cmd_text(argv: List[str]) -> str:
    return " ".join(argv).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_path(repo: Path, name: str) -> Optional[Path]:
    """``rev-parse --git-path <name>`` resolved to an absolute path, or ``None``."""
    try:
        res = run_git(["-C", str(repo), "rev-parse", "--git-path", name], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    path = Path(res.stdout.strip())
    return path if path.is_absolute() else repo / path


def tree_hash(repo: Path) -> Tuple[Optional[str], str]:
    """``(tree_sha, "")`` for the whole working tree, or ``(None, reason)``."""
    index = git_path(repo, "index")
    if index is None:
        return None, "not a git working tree"
    fd, tmp_name = tempfile.mkstemp(prefix="gate_evidence_index_")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if index.exists():
            # copy2, not copyfile: git decides an entry is "racily clean" (its content must be re-read, its stat
            # cannot be trusted) by comparing the entry's mtime to the INDEX FILE's mtime, at one-second
            # resolution on Git for Windows. A copy stamped "now" erases that, so a same-size rewrite in the
            # same second as the last index write read as unchanged and FRESH.
            runtime_files.copy_with_metadata(index, tmp)
        else:
            tmp.unlink()  # a fresh repo has no index yet; git creates the temp one
        env = {"GIT_INDEX_FILE": str(tmp)}
        try:
            added = run_git(["-C", str(repo), "add", "-A"], timeout=HASH_TIMEOUT_SECONDS, extra_env=env)
            if added.returncode != 0:
                return None, "git add -A failed: " + (added.stderr.strip().splitlines() or ["?"])[0][:200]
            tree = run_git(["-C", str(repo), "write-tree"], timeout=HASH_TIMEOUT_SECONDS, extra_env=env)
        except subprocess.TimeoutExpired:
            return None, f"hashing the tree took over {HASH_TIMEOUT_SECONDS}s"
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"git failed: {type(exc).__name__}"
        if tree.returncode != 0 or not tree.stdout.strip():
            return None, "git write-tree failed: " + (tree.stderr.strip().splitlines() or ["?"])[0][:200]
        return tree.stdout.strip(), ""
    finally:
        for leftover in (tmp, Path(str(tmp) + ".lock")):
            try:
                leftover.unlink()
            except OSError:
                pass


def changed_paths(repo: Path, old: str, new: str, limit: int = 10) -> List[str]:
    """Paths that differ between two tree objects (best-effort, capped)."""
    try:
        res = run_git(["-C", str(repo), "diff-tree", "-r", "--name-only", old, new], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    paths = [p for p in res.stdout.splitlines() if p.strip()] if res.returncode == 0 else []
    return paths[:limit] + ([f"… {len(paths) - limit} more"] if len(paths) > limit else [])


def _load(path: Path) -> Dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(path: Path, data: Dict[str, dict]) -> None:
    hooks_state.atomic_write_text(path, json.dumps(data, indent=2))


def _handle(stream) -> Optional[int]:
    """The OS file descriptor behind ``stream``, or ``None`` (a StringIO)."""
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _inflight_dir() -> Path:
    return hooks_state.state_dir() / GATE_INFLIGHT_DIRNAME


@contextlib.contextmanager
def inflight_marker(repo: Path, label: str) -> Iterator[None]:
    """Keep ``gate-inflight/<pid>.json`` for the gate's lifetime; never fails the gate."""
    path: Optional[Path] = _inflight_dir() / f"{os.getpid()}.json"
    body = {"pid": os.getpid(), "repo": str(repo), "label": label,
            "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        runtime_files.make_dirs(path.parent, parents=True, exist_ok=True)
        # A hard-killed gate never reaches its `finally`; reap markers whose pid
        # is confirmed gone so a reused pid cannot revive one.
        for stale in path.parent.glob("*.json"):
            if stale.stem.isdigit() and pid_state(int(stale.stem)) is False:
                with contextlib.suppress(OSError):
                    stale.unlink()
        hooks_state.atomic_write_text(path, json.dumps(body))
    except OSError:
        path = None
    try:
        yield
    finally:
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def gate_in_flight(cwd: Path) -> Optional[bool]:
    """Is a gate running for the tree holding ``cwd``? ``None`` when that can't be told."""
    try:
        entries = list(_inflight_dir().glob("*.json"))
    except OSError:
        return None
    here = _norm(str(cwd))
    for entry in entries:
        try:
            row = json.loads(entry.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # the gate finished between the listing and the read
        except (OSError, ValueError):
            return None
        if not isinstance(row, dict):
            return None
        repo, pid = _norm(str(row.get("repo") or "")), row.get("pid")
        if not row.get("repo") or not (here == repo or here.startswith(repo + os.sep)):
            continue
        if isinstance(pid, int) and not isinstance(pid, bool) and pid_state(pid) is False:
            continue  # a gate that died without removing its marker
        return True
    return False


def run(repo: Path, label: str, argv: List[str]) -> Tuple[int, str]:
    """Run ``argv`` in ``repo``; ``(exit_code, EVIDENCE=... line)``."""
    before, reason = tree_hash(repo)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        # Explicit handles: with CREATE_NO_WINDOW and none passed, a console
        # child loses the inherited stdout/stderr and the gate's own FAIL
        # lines vanish while its exit code still arrives.
        with inflight_marker(repo, label):
            # subprocess-timeout-exempt: foreground runner for the caller's gate, whose length is the caller's to set
            proc = subprocess.run(argv, cwd=str(repo), creationflags=NO_WINDOW,
                                  stdout=_handle(sys.stdout), stderr=_handle(sys.stderr))
        code = proc.returncode
    except OSError as exc:
        print(f"gate_evidence: could not start {argv[0]!r}: {exc}", file=sys.stderr)
        code = 127
    after, after_reason = tree_hash(repo) if before else (None, reason)

    evidence_path = git_path(repo, EVIDENCE_NAME)
    if evidence_path is None:
        return code, "EVIDENCE=not_recorded reason=not a git working tree"
    data = _load(evidence_path)
    if before is None or after is None:
        why = reason or after_reason
    elif before != after:
        why = "tree changed during the run: " + ", ".join(changed_paths(repo, before, after))
    else:
        why = ""
    if why:
        data.pop(label, None)
        _store(evidence_path, data)
        return code, f"EVIDENCE=not_recorded label={label} reason={why}"
    cmd = _cmd_text(argv)
    data[label] = {"cmd": cmd, "cmd_sha256": _sha(cmd), "exit": code, "wtree": after,
                   "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    _store(evidence_path, data)
    return code, f"EVIDENCE=recorded label={label} exit={code} wtree={after[:12]}"


def check(repo: Path, label: str, expect_cmd: Optional[str] = None) -> Tuple[str, str]:
    """``(FRESH|STALE|MISSING|UNKNOWN, detail)`` for ``label`` against the current tree."""
    current, reason = tree_hash(repo)
    if current is None:
        return "UNKNOWN", reason
    evidence_path = git_path(repo, EVIDENCE_NAME)
    record = _load(evidence_path).get(label) if evidence_path else None
    if not isinstance(record, dict) or record.get("exit") != 0:
        return "MISSING", "no recorded passing run for " + label
    if expect_cmd is not None and record.get("cmd_sha256") != _sha(expect_cmd.strip()):
        return "MISSING", f"recorded command differs: {record.get('cmd')!r}"
    if record.get("wtree") != current:
        paths = changed_paths(repo, str(record.get("wtree")), current)
        return "STALE", "changed since the recorded run: " + (", ".join(paths) or "unknown paths")
    return "FRESH", f"recorded {record.get('utc')}: {record.get('cmd')}"


def check_red(repo: Path, label: str = "red") -> Tuple[str, str]:
    """``(RED|NOT_RED|SAME_TREE|MISSING|UNKNOWN, detail)``: did ``label`` fail on a tree other than this one?

    A bug fix's regression test must be seen failing before the fix. ``run`` records a failing exit too, so the
    record is the proof, provided it was taken on a different tree (the pre-fix one) than the tree being shipped.
    Exit 127 means the command never started, which proves nothing about the code.
    """
    current, reason = tree_hash(repo)
    if current is None:
        return "UNKNOWN", reason
    evidence_path = git_path(repo, EVIDENCE_NAME)
    record = _load(evidence_path).get(label) if evidence_path else None
    if not isinstance(record, dict) or not isinstance(record.get("wtree"), str):
        return "MISSING", f"no recorded run for {label}"
    code = record.get("exit")
    if not isinstance(code, int) or isinstance(code, bool) or code in (0, 127):
        return "NOT_RED", f"the recorded run of {label} did not fail (exit {code!r})"
    if record.get("wtree") == current:
        return "SAME_TREE", f"the failing run of {label} was on this very tree; it must be a different tree (the pre-fix one)"
    return "RED", f"recorded {record.get('utc')}: exit {code} on {str(record['wtree'])[:12]}, fix tree {current[:12]}"


_CHECK_EXIT = {"FRESH": 0, "STALE": 1, "MISSING": 1, "UNKNOWN": 2}
_CHECK_RED_EXIT = {"RED": 0, "NOT_RED": 1, "SAME_TREE": 1, "MISSING": 1, "UNKNOWN": 2}


def main(argv: Optional[List[str]] = None) -> int:
    ensure_utf8_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)
    command: List[str] = []
    if "--" in raw:
        split = raw.index("--")
        raw, command = raw[:split], raw[split + 1:]
    parser = argparse.ArgumentParser(description="Gate evidence bound to a tree hash (fleet-config#957).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--label", required=True)
    run_p.add_argument("--repo", type=Path, default=Path.cwd())
    chk = sub.add_parser("check")
    chk.add_argument("--label", required=True)
    chk.add_argument("--repo", type=Path, default=Path.cwd())
    chk.add_argument("--expect-cmd")
    red = sub.add_parser("check-red")
    red.add_argument("--label", default="red")
    red.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(raw)
    repo = args.repo.resolve()

    if args.cmd == "run":
        if not command:
            parser.error("run needs a command after --")
        code, line = run(repo, args.label, command)
        sys.stdout.flush()
        print(line, flush=True)
        return code
    if args.cmd == "check-red":
        state, detail = check_red(repo, args.label)
        print(state)
        print(detail, file=sys.stderr)
        return _CHECK_RED_EXIT[state]
    state, detail = check(repo, args.label, args.expect_cmd)
    print(state)
    print(detail, file=sys.stderr)
    return _CHECK_EXIT[state]


if __name__ == "__main__":
    sys.exit(main())
