"""`owner_check` command line: the one entry point every skill and the chief call.

Each command prints `KEY=value` lines. `classify`/`finish` exit 0 for none, 1 for
required, 3 for unknown (an input could not be read, or a write did not land),
2 for a refused call. `gate` exits 0 clear, 1 blocked, 3 unknown. `mark`, `clear`,
`ensure-labels` and `backfill` exit 0 when every write landed, 3 otherwise;
`mark` exits 2 for a before-start step with no concrete owner action.
Contract: `docs/owner-check.md`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

_LIB = Path(__file__).resolve().parent.parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from fleet_core.utf8_stdio import ensure_utf8_stdio  # noqa: E402

from owner_check import backfill, block, github, reclassify, repair, rule  # noqa: E402

EXIT_NONE, EXIT_REQUIRED, EXIT_REFUSED, EXIT_UNKNOWN = 0, 1, 2, 3


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ref(text: str) -> tuple:
    """Parse `<repo>#<N>` or `<owner>/<repo>#<N>` into a bare `(repo, N)`.

    The github seam prefixes the fleet owner itself, so a qualified ref keeps only
    its repo; a different owner is refused rather than silently read as ours.
    """
    expected = f"expected <repo>#<N> or <owner>/<repo>#<N>, got {text!r}"
    name, sep, num = text.partition("#")
    parts = name.split("/")
    if not sep or not num.isdigit() or len(parts) > 2 or not all(parts):
        raise argparse.ArgumentTypeError(expected)
    if len(parts) == 2 and parts[0] != github.OWNER:
        raise argparse.ArgumentTypeError(
            f"owner {parts[0]!r} is not the fleet owner {github.OWNER!r}; {expected}")
    return parts[-1], int(num)


def _print_reasons(reasons: Sequence[str]) -> None:
    for reason in reasons:
        print(f"REASON={reason}")


def cmd_classify(args: argparse.Namespace) -> int:
    try:
        body = Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"OWNER_CHECK=unknown reason=cannot read {args.body_file}: {exc}")
        return EXIT_UNKNOWN
    decision = rule.decide(args.label, args.title, body)
    print(f"OWNER_CHECK={decision.verdict}")
    _print_reasons(decision.reasons)
    return EXIT_REQUIRED if decision.verdict == rule.REQUIRED else EXIT_NONE


def _finish_refusal(args: argparse.Namespace) -> Optional[str]:
    """Why `finish` writes nothing, or None. Checked before any read or write."""
    if args.verified is None:
        return "--verified is required: yes, scheduled, or no with a --gap per unprovable criterion"
    if args.verified != rule.VERIFIED_NO:
        return "--gap only goes with --verified no" if args.gap else None
    if not args.gap:
        return ("--verified no needs a --gap <class>:<criterion> per criterion the session cannot prove, "
                f"class one of {', '.join(rule.OWNER_CLASSES)}; prove the rest with the gate, the repro, "
                f"{'; '.join(rule.PROOF_TOOLS)} and pass --verified yes")
    try:
        args.gaps = [rule.parse_gap(text) for text in args.gap]
    except ValueError as exc:
        return str(exc)
    return None


def cmd_finish(args: argparse.Namespace) -> int:
    repo, number = args.ref
    refusal = _finish_refusal(args)
    if refusal:
        print(f"OWNER_CHECK=refused APPLIED=no reason={refusal}")
        return EXIT_REFUSED
    try:
        issue = github.fetch_issue(repo, number)
        files = github.pr_files(repo, args.pr)
        proven = rule.ux_proven(github.pr_body(repo, args.pr))
    except github.GhError as exc:
        print(f"OWNER_CHECK=unknown reason=could not read the issue or PR: {exc}")
        return EXIT_UNKNOWN
    if args.verified == rule.VERIFIED_SCHEDULED:
        print("SCHEDULED_READ=chief reason=a criterion only a later scheduled run can show")
    surface = rule.unproven_surface(issue["labels"], files, proven)
    if surface:
        print(f"OWNER_CHECK=refused APPLIED=no reason=adds a new page or view ({surface}) with no visual proof; "
              "a browser proves it, not the owner: run it and put VISUAL_PROOF=fresh in the PR body, then re-run")
        return EXIT_REFUSED
    decision = rule.decide(issue["labels"], issue["title"], issue["body"], files, args.verified,
                           getattr(args, "gaps", ()))
    latest = block.latest_block(issue["comments"]) or {}
    label = block.LABELS[block.VALIDATE_AFTER]
    if decision.verdict == rule.NONE:
        print("OWNER_CHECK=none")
        return _retract_own_mark(repo, number, issue, latest, args.pr)
    _print_reasons(decision.reasons)
    if label in issue["labels"] and latest.get("kind") == block.VALIDATE_AFTER:
        print(f"OWNER_CHECK=required APPLIED=already label={label}")
        return EXIT_REQUIRED
    if not args.try_text:
        print("OWNER_CHECK=required APPLIED=no reason=--try is required when the rule says required; re-run with it")
        return EXIT_REFUSED
    proof = [github.pr_url(repo, args.pr), *args.proof]
    body = block.format_block(block.VALIDATE_AFTER, "; ".join(decision.reasons), args.try_text,
                              proof, _today())
    try:
        github.mark(repo, number, block.VALIDATE_AFTER, body)
    except github.GhError as exc:
        print(f"OWNER_CHECK=unknown reason=required, but the label or comment did not land: {exc}")
        return EXIT_UNKNOWN
    print(f"OWNER_CHECK=required APPLIED=yes label={label}")
    return EXIT_REQUIRED


def _retract_own_mark(repo: str, number: int, issue: dict, latest: dict, pr: int) -> int:
    """A `none` re-run drops the label an earlier `finish` for the same PR put on.

    Only a validate-after block whose proof leads with this PR's url is `finish`'s own;
    a hand mark or another PR's block is left for the Board.
    """
    label = block.LABELS[block.VALIDATE_AFTER]
    proof = latest.get("proof") or []
    if (label not in issue["labels"] or latest.get("kind") != block.VALIDATE_AFTER
            or not proof or proof[0] != github.pr_url(repo, pr)):
        return EXIT_NONE
    body = block.format_block(block.CLEARED, "a later finish for the same PR found no owner-check reason", "",
                              (github.pr_url(repo, pr),), _today(),
                              answer="re-run of owner_check finish: none; the earlier mark is retracted")
    try:
        github.clear(repo, number, [label], body)
    except github.GhError as exc:
        print(f"RETRACTED=unknown reason=the earlier mark could not be removed: {exc}")
        return EXIT_UNKNOWN
    print(f"RETRACTED=yes label={label}")
    return EXIT_NONE


def cmd_gate(args: argparse.Namespace) -> int:
    repo, number = args.ref
    try:
        issue = github.fetch_issue(repo, number)
    except github.GhError as exc:
        print(f"OWNER_GATE=unknown reason=could not read the issue: {exc}")
        return EXIT_UNKNOWN
    if block.LABELS[block.BEFORE_START] not in issue["labels"]:
        print("OWNER_GATE=clear")
        return EXIT_NONE
    latest = block.latest_block(issue["comments"]) or {}
    found = latest.get("kind") == block.BEFORE_START
    reason = latest.get("reason") if found else "labelled needs-owner with no owner-check block"
    print(f"OWNER_GATE=blocked reason={reason}")
    if found and latest.get("try"):
        print(f"TRY={latest['try']}")
    return EXIT_REQUIRED


def cmd_mark(args: argparse.Namespace) -> int:
    repo, number = args.ref
    for name, value in (("reason", args.reason), ("try", args.try_text)):
        if not value.strip():
            print(f"MARKED=refused reason=--{name} is empty; a label with no {name} shows on the Board as "
                  "'details unknown'")
            return EXIT_REFUSED
    if args.kind == block.BEFORE_START:
        problem = block.vague_action(args.try_text)
        if problem:
            print(f"MARKED=refused reason=--try {problem}; name what the owner does, where, "
                  "and which answer unblocks the work (an agent's own decision goes to the chief)")
            return EXIT_REFUSED
    body = block.format_block(args.kind, args.reason, args.try_text, args.proof, _today(),
                              related=args.related)
    try:
        label = github.mark(repo, number, args.kind, body)
    except github.GhError as exc:
        print(f"MARKED=unknown reason={exc}")
        return EXIT_UNKNOWN
    print(f"MARKED={repo}#{number} label={label}")
    return EXIT_NONE


def cmd_clear(args: argparse.Namespace) -> int:
    repo, number = args.ref
    try:
        issue = github.fetch_issue(repo, number)
        body = block.format_block(block.CLEARED, "answered on the Board", "", (), _today(),
                                  answer=args.answer)
        removed = github.clear(repo, number, issue["labels"], body)
    except github.GhError as exc:
        print(f"CLEARED=unknown reason={exc}")
        return EXIT_UNKNOWN
    print(f"CLEARED={repo}#{number} removed={','.join(removed) or 'none'}")
    return _clear_siblings(repo, number, issue, args.answer)


def _clear_siblings(repo: str, number: int, issue: dict, answer: str) -> int:
    """`list` merged same-`try` items into this one's Board line, so its answer clears them too."""
    latest = block.latest_block(issue["comments"]) or {}
    kind = latest.get("kind")
    if kind not in block.LABELS or block.LABELS[kind] not in issue["labels"]:
        return EXIT_NONE
    try:
        siblings = github.same_try_siblings(repo, number, kind, str(latest.get("try") or ""))
        for sibling in siblings:
            body = block.format_block(block.CLEARED, f"answered on the Board with {repo}#{number} (same try)",
                                      "", (), _today(), answer=answer)
            github.clear(repo, sibling, [block.LABELS[kind]], body)
            print(f"MERGED_CLEARED with={repo}#{number} also={repo}#{sibling}")
    except github.GhError as exc:
        print(f"CLEARED=unknown reason=a merged same-try item did not clear: {exc}")
        return EXIT_UNKNOWN
    return EXIT_NONE


def cmd_list(args: argparse.Namespace) -> int:
    result = github.list_items(args.repo or None)
    result["items"] = github.merge_same_try(result["items"])
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        for it in result["items"]:
            print(f"OWNER_ITEM {it['kind']} {it['ref']} since={it['since'] or '?'} reason={it['reason'] or '(no block)'}"
                  + (f" merged={','.join(it['merged'])}" if it.get('merged') else ""))
        for miss in result["unknown"]:
            print(f"UNKNOWN={miss['repo']} reason={miss['reason']}")
        print(f"ITEMS={len(result['items'])} UNKNOWN={len(result['unknown'])}")
    return EXIT_UNKNOWN if result["unknown"] else EXIT_NONE


def cmd_ensure_labels(args: argparse.Namespace) -> int:
    try:
        repos = args.repo or github.fleet_repo_names()
    except RuntimeError as exc:
        print(f"LABELS=unknown reason={exc}")
        return EXIT_UNKNOWN
    failed = 0
    for repo in repos:
        for label in block.LABEL_STYLE:
            try:
                github.ensure_label(repo, label)
            except github.GhError as exc:
                failed += 1
                print(f"FAILED={repo} label={label} reason={exc}")
    print(f"LABELS=done repos={len(repos)} failed={failed}")
    return EXIT_UNKNOWN if failed else EXIT_NONE


def _collect_backfill(args: argparse.Namespace) -> tuple:
    audit = backfill.parse_audit_list(Path(args.list).read_text(encoding="utf-8")) if args.list else []
    ruled: List[backfill.Item] = []
    unknown: List[str] = []
    if args.since:
        try:
            repos = args.repo or github.fleet_repo_names()
        except RuntimeError as exc:
            return audit, ruled, [f"* {exc}"]
        for repo in repos:
            try:
                ruled += backfill.rule_candidates(repo, github.closed_issues(repo), args.since, args.until)
            except (github.GhError, ValueError) as exc:
                unknown.append(f"{repo} {exc}")
    return audit, ruled, unknown


def _backfill_one(item: backfill.Item, apply: bool) -> str:
    """One item's outcome word: planned, skipped, marked or failed."""
    backfill.resolve_anchor(item, github.is_pull_request)
    if not item.numbers:
        return "skipped:all-prs"
    issue = github.fetch_issue(item.repo, item.anchor)
    if block.open_kinds(issue["labels"]) or block.latest_block(issue["comments"]):
        return "skipped:has-owner-check"
    if not apply:
        return "planned"
    reason = "; ".join([backfill.BACKFILL_REASON, *(r.split(" (")[0] for r in item.reasons)])
    body = block.format_block(block.VALIDATE_AFTER, reason, backfill.BACKFILL_TRY, issue["prs"],
                              _today(), related=item.related)
    github.mark(item.repo, item.anchor, block.VALIDATE_AFTER, body)
    return "marked"


def _reclassify_one(item: dict, apply: bool) -> str:
    """One validate-after item's outcome line under the current rule."""
    repo, number = item["repo"], item["number"]
    issue = github.fetch_issue(repo, number)
    if block.LABELS[block.BEFORE_START] in issue["labels"]:
        return f"RECLASS keep {item['ref']} reason=also needs-owner; left alone"
    verdict = reclassify.reclassify(item, issue, lambda pr: github.pr_files(repo, pr))
    if verdict.state == reclassify.UNKNOWN:
        return f"RECLASS unknown {item['ref']} reason={verdict.why}"
    if verdict.state == reclassify.KEEP:
        return f"RECLASS keep {item['ref']} reason={'; '.join(verdict.kept)}"
    words = "; ".join(reclassify.DROPPED_WORDS[key] for key in verdict.dropped) or "no reason fires"
    if apply:
        label = block.LABELS[block.VALIDATE_AFTER]
        cleared = block.format_block(block.CLEARED, "re-checked under the tightened owner-check rule", "", (),
                                     _today(), answer=f"no owner-check reason fires any more: {words}")
        github.clear(repo, number, [label], cleared)
    return f"RECLASS {'cleared' if apply else 'would-clear'} {item['ref']} dropped={words}"


def cmd_reclassify(args: argparse.Namespace) -> int:
    result = github.list_items(args.repo or None)
    todo = [it for it in result["items"] if it["kind"] == block.VALIDATE_AFTER]
    counts = {"keep": 0, "clear": 0, "unknown": len(result["unknown"])}
    for item in todo:
        try:
            line = _reclassify_one(item, args.apply)
        except github.GhError as exc:
            line = f"RECLASS unknown {item['ref']} reason={exc}"
        print(line)
        word = line.split()[1]
        counts["clear" if word in ("cleared", "would-clear") else "keep" if word == "keep" else "unknown"] += 1
    for miss in result["unknown"]:
        print(f"UNKNOWN={miss['repo']} reason={miss['reason']}")
    print(f"RECLASSIFY={'applied' if args.apply else 'dry-run'} before={len(todo)} "
          f"clear={counts['clear']} keep={counts['keep']} unknown={counts['unknown']}")
    return EXIT_UNKNOWN if counts["unknown"] else EXIT_NONE


_PR_URL = re.compile(r"/pull/(\d+)$")


def _repair_one(item: dict, apply: bool) -> str:
    """One unreadable item's outcome line: planned, repaired, unbuildable or skipped."""
    repo, number, kind = item["repo"], item["number"], item["kind"]
    issue = github.fetch_issue(repo, number)
    latest = block.latest_block(issue["comments"]) or {}
    if block.LABELS[kind] not in issue["labels"] or latest.get("kind") == kind:
        return f"REPAIR skipped {item['ref']} reason=already readable or no longer labelled"
    bodies = [("issue", issue["body"])]
    for url in issue["prs"]:
        m = _PR_URL.search(url)
        if m:
            bodies.append(("pr", github.pr_body(repo, int(m.group(1)))))
    built = repair.build_try(kind, bodies)
    if built is None:
        return f"REPAIR unbuildable {item['ref']} reason=a {kind} note needs a concrete owner action and none is on the issue"
    try_text, source = built
    if apply:
        body = block.format_block(kind, repair.REPAIR_REASON, try_text, issue["prs"], _today())
        github.note(repo, number, body)
    return f"REPAIR {'repaired' if apply else 'planned'} {item['ref']} source={source}"


def cmd_repair(args: argparse.Namespace) -> int:
    result = github.list_items(args.repo or None)
    todo = [it for it in result["items"] if not it["block"]]
    counts = {"planned": 0, "repaired": 0, "unbuildable": 0, "skipped": 0, "unknown": len(result["unknown"])}
    for item in todo:
        try:
            line = _repair_one(item, args.apply)
        except github.GhError as exc:
            line = f"REPAIR unknown {item['ref']} reason={exc}"
        print(line)
        counts[line.split()[1]] += 1
    for miss in result["unknown"]:
        print(f"UNKNOWN={miss['repo']} reason={miss['reason']}")
    fixable = counts["repaired"] + counts["planned"]
    skipped = f" skipped={counts['skipped']}" if counts["skipped"] else ""
    print(f"REPAIR={'applied' if args.apply else 'dry-run'} before={len(todo)} repairable={fixable} "
          f"unbuildable={counts['unbuildable']} unknown={counts['unknown']}{skipped}")
    return EXIT_UNKNOWN if counts["unbuildable"] or counts["unknown"] else EXIT_NONE


def cmd_backfill(args: argparse.Namespace) -> int:
    audit, ruled, unknown = _collect_backfill(args)
    items = backfill.merge(audit, ruled)
    counts: dict = {}
    for item in items:
        try:
            outcome = _backfill_one(item, args.apply)
        except github.GhError as exc:
            outcome = "failed"
            unknown.append(f"{item.repo}#{item.anchor} {exc}")
        counts.setdefault(item.repo, {}).setdefault(outcome, 0)
        counts[item.repo][outcome] += 1
        related = f" related={','.join(item.related)}" if item.related else ""
        print(f"ITEM {outcome} {item.repo}#{item.anchor if item.numbers else '-'} source={item.source}{related}")
    for repo in sorted(counts):
        print(f"REPO {repo} " + " ".join(f"{k}={v}" for k, v in sorted(counts[repo].items())))
    for miss in unknown:
        print(f"UNKNOWN={miss}")
    print(f"BACKFILL={'applied' if args.apply else 'dry-run'} items={len(items)} unknown={len(unknown)}")
    return EXIT_UNKNOWN if unknown else EXIT_NONE


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="owner_check", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="the rule over an issue draft (/issue-add)")
    c.add_argument("--body-file", required=True)
    c.add_argument("--title", default="")
    c.add_argument("--label", action="append", default=[])
    c.set_defaults(func=cmd_classify)

    f = sub.add_parser("finish", help="after merge: decide, then label + block when required (/issue-finish)")
    f.add_argument("ref", type=_ref)
    f.add_argument("--pr", type=int, required=True)
    f.add_argument("--verified", choices=(rule.VERIFIED_YES, rule.VERIFIED_SCHEDULED, rule.VERIFIED_NO),
                   default=None,
                   help="yes = the original repro / acceptance re-ran and passed in this session; "
                        "scheduled = all of it did except a criterion only a later scheduled run shows; "
                        "no = a --gap names each criterion only the owner can prove")
    f.add_argument("--gap", action="append", default=[],
                   help=f"<class>:<criterion>, class one of {', '.join(rule.OWNER_CLASSES)} (with --verified no)")
    f.add_argument("--try", dest="try_text", default="")
    f.add_argument("--proof", action="append", default=[])
    f.set_defaults(func=cmd_finish)

    g = sub.add_parser("gate", help="refuse to start a needs-owner issue (/issue-start)")
    g.add_argument("ref", type=_ref)
    g.set_defaults(func=cmd_gate)

    m = sub.add_parser("mark", help="label + block by hand (chief, back-fill)")
    m.add_argument("ref", type=_ref)
    m.add_argument("--kind", required=True, choices=(block.VALIDATE_AFTER, block.BEFORE_START))
    m.add_argument("--reason", required=True)
    m.add_argument("--try", dest="try_text", required=True)
    m.add_argument("--proof", action="append", default=[])
    m.add_argument("--related", action="append", default=[])
    m.set_defaults(func=cmd_mark)

    cl = sub.add_parser("clear", help="Roberto answered on the Board: drop the label, post the cleared block")
    cl.add_argument("ref", type=_ref)
    cl.add_argument("--answer", required=True)
    cl.set_defaults(func=cmd_clear)

    ls = sub.add_parser("list", help="every open owner item across the fleet")
    ls.add_argument("--repo", action="append", default=[])
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    el = sub.add_parser("ensure-labels", help="create both labels in every fleet repo (idempotent)")
    el.add_argument("--repo", action="append", default=[])
    el.set_defaults(func=cmd_ensure_labels)

    rc = sub.add_parser("reclassify", help="re-run the rule over every open needs-validation item (dry run unless --apply)")
    rc.add_argument("--repo", action="append", default=[])
    rc.add_argument("--apply", action="store_true")
    rc.set_defaults(func=cmd_reclassify)

    rp = sub.add_parser("repair", help="write the missing note on every labelled issue with none (dry run unless --apply)")
    rp.add_argument("--repo", action="append", default=[])
    rp.add_argument("--apply", action="store_true")
    rp.set_defaults(func=cmd_repair)

    b = sub.add_parser("backfill", help="label shipped-but-unvalidated work once (dry run unless --apply)")
    b.add_argument("--list", default=None, help="audit list file of repo#N items")
    b.add_argument("--since", default=None, help="rule dry run over issues closed from this date (YYYY-MM-DD)")
    b.add_argument("--until", default=_today())
    b.add_argument("--repo", action="append", default=[])
    b.add_argument("--apply", action="store_true")
    b.set_defaults(func=cmd_backfill)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    ensure_utf8_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
