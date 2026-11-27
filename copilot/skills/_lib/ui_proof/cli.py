"""CLI for the visual-proof store: `capture-before`, `capture-after`, `build` (see the package docstring)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

import active_issue
from fleet_core.utf8_stdio import ensure_utf8_stdio

from ui_proof import mr_line, proof, store, walkthrough


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ui_proof", description="Visual proof for a UI change.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for phase in store.PHASES:
        cap = sub.add_parser(f"capture-{phase}", help=f"stage the {phase} screenshots in .proof/")
        cap.add_argument("repo", type=Path)
        cap.add_argument("--manifest", type=Path, default=None, help="a layout-lint capture manifest")
        cap.add_argument("--image", action="append", default=[], help="'<state>:<viewport>-<theme>=<png>', repeatable")
    bld = sub.add_parser("build", help="write .proof/<MR>/index.html and declare VISUAL_PROOF= on the MR")
    bld.add_argument("repo", type=Path)
    bld.add_argument("--mr", type=int, required=True, help="the merge request number")
    bld.add_argument("--criterion", action="append", default=[],
                     help="'<state>=<what changed and where to look>', repeatable")
    bld.add_argument("--no-visual", action="append", default=[], help="a criterion with no visual change")
    bld.add_argument("--title", default="")
    bld.add_argument("--issue", type=int, default=None)
    when = bld.add_mutually_exclusive_group()
    when.add_argument("--no-ui", action="store_true", help="nothing visible changed: declare n/a, write no report")
    when.add_argument("--sha", default="", help="the merge sha, once merged: stamp the report and publish it")
    return ap


def _capture(repo: Path, args: argparse.Namespace) -> int:
    try:
        images = store.parse_images(args.image)
    except ValueError as exc:
        print(f"CAPTURED=none reason={exc}")
        return 2
    ok, line = store.capture(repo, args.cmd.split("-", 1)[1], args.manifest, images)
    print(line)
    return 0 if ok else 1


def _build(repo: Path, args: argparse.Namespace, glab: Optional[mr_line.Glab]) -> int:
    # The Board looks a report up under the repo's folder name, which is its GitLab project name.
    name = active_issue.resolve_repo_name(repo)
    if args.sha:
        ok, lines = walkthrough.stamp(repo, name, args.mr, args.sha)
        print("\n".join(lines))
        return 0 if ok else 1
    if args.no_ui:
        state, lines = proof.NOT_APPLICABLE, ["PROOF_PAGE=none reason=no UI change"]
    else:
        try:
            criteria = proof.parse_criteria(args.criterion)
        except ValueError as exc:
            print(f"PROOF_PAGE=not_built reason={exc}")
            return 2
        state, lines = walkthrough.build(repo, name, args.mr, criteria, args.no_visual, args.title, args.issue)
    declared, line = mr_line.set_line(repo, args.mr, state, glab)
    print("\n".join([*lines, f"VISUAL_PROOF={state}", line]))
    built = args.no_ui or not lines[0].startswith("PROOF_PAGE=not_built")
    return 0 if built and declared else 1


def main(argv: Optional[List[str]] = None, glab: Optional[mr_line.Glab] = None) -> int:
    ensure_utf8_stdio()
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    return _build(repo, args, glab) if args.cmd == "build" else _capture(repo, args)
