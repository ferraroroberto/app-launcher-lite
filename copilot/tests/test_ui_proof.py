"""Tests for ui_proof: what the writer stores is what the Board reads, and the state it declares is honest.

Run:  python -m unittest discover -s tests -v

Each test works in a real git repo under a temp projects folder; glab is a fake
that holds one merge request description. The store is read back through the
app's own reader and GitLab client, so the writer cannot drift from them.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

COPILOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COPILOT / "skills" / "_lib"))
sys.path.insert(0, str(COPILOT.parent))

import git_run  # noqa: E402
from ui_proof import cli, mr_line, proof, walkthrough  # noqa: E402

from src.board import proof_reports  # noqa: E402
from src.clients import gitlab  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nsynthetic"


class FakeGlab:
    """One merge request: `mr view` shows its description, `mr update` replaces it."""

    def __init__(self, description: str = "Closes #7", keeps_updates: bool = True) -> None:
        self.description, self.keeps_updates, self.updates = description, keeps_updates, 0

    def __call__(self, args: list) -> subprocess.CompletedProcess:
        if args[:2] == ["mr", "view"]:
            body = json.dumps({"iid": int(args[2]), "description": self.description})
            return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")
        self.updates += 1
        if self.keeps_updates:
            # The bytes as glab would send them: universal newlines would hide a doubled carriage return.
            self.description = Path(args[args.index("--description-file") + 1]).read_bytes().decode("utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def git(repo: Path, *args: str) -> str:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    return git_run.run_git(["-C", str(repo), *identity, *args], check=True, timeout=60).stdout


class ProofTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.projects = self.tmp / "projects"
        self.repo = self.projects / "demo-project"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        # A user-level core.hooksPath (author allowlists, signing) must not reach a throwaway repo.
        git(self.repo, "config", "core.hooksPath", str(self.tmp / "no-hooks"))
        (self.repo / "app.txt").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "app.txt")
        git(self.repo, "commit", "-q", "-m", "start")
        git(self.repo, "checkout", "-q", "-b", "feat/7-x")
        self.shot = self.tmp / "shot.png"
        self.shot.write_bytes(PNG)

    def run_cli(self, *argv: str, glab: FakeGlab | None = None) -> tuple:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv), glab)
        return code, out.getvalue()

    def capture(self, phase: str, repo: Path | None = None) -> None:
        code, out = self.run_cli(f"capture-{phase}", str(repo or self.repo),
                                 "--image", f"board.done:phone-light={self.shot}")
        self.assertEqual(code, 0, out)

    def build(self, glab: FakeGlab, *extra: str, repo: Path | None = None) -> tuple:
        return self.run_cli("build", str(repo or self.repo), "--mr", "21", *extra, glab=glab)

    def stored(self) -> Path | None:
        return proof_reports.report_path("demo-project", 21, str(self.projects))


class RoundTripTests(ProofTestCase):
    def test_the_reader_and_the_gitlab_client_take_what_the_writer_wrote(self) -> None:
        glab = FakeGlab()
        self.capture("before")
        (self.repo / "app.txt").write_text("two\n", encoding="utf-8")
        self.capture("after")
        code, out = self.build(glab, "--criterion", "board.done=<b>the chip</b>")
        self.assertEqual(code, 0, out)
        self.assertIn("VISUAL_PROOF=fresh\nMR_LINE=written", out)

        page = self.stored()
        self.assertEqual(page, self.repo / ".proof" / "21" / "index.html")
        text = page.read_text(encoding="utf-8")
        self.assertIn("&lt;b&gt;the chip&lt;/b&gt;", text)
        self.assertEqual(text.count("data:image/png;base64,"), 2)
        self.assertNotIn("<script", text)

        # The screenshots never show as changes, and no tracked file was edited to hide them.
        self.assertEqual(git(self.repo, "status", "--porcelain").split(), ["M", "app.txt"])
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.splitlines().count(".proof/"), 1)

        row = {"iid": 21, "merged_at": datetime.now(timezone.utc).isoformat(), "description": glab.description,
               "web_url": "u", "references": {"full": "group/demo-project!21"}}
        with mock.patch.object(gitlab, "_api_list", return_value=[row]):
            closers = gitlab.search_merged_on("group", datetime.now(timezone.utc).date(), timezone.utc)
        (closer,) = closers.values()
        self.assertEqual((closer["pr"], closer["visual_proof"]), (21, "fresh"))
        self.assertEqual([issue for _, issue in closers], [7])

    def test_a_worktree_report_reaches_the_primary_only_with_the_merge_sha(self) -> None:
        worktree = self.projects / "demo-project-wt-7"
        git(self.repo, "worktree", "add", "-q", "-b", "feat/7-wt", str(worktree))
        glab = FakeGlab()
        self.capture("after", worktree)
        self.assertEqual(self.build(glab, repo=worktree)[0], 0)
        self.assertIsNone(self.stored())

        code, out = self.build(glab, "--sha", "abc123", repo=worktree)
        self.assertEqual(code, 0, out)
        self.assertIn("merge <code>abc123</code>", self.stored().read_text(encoding="utf-8"))
        index = json.loads((self.repo / ".proof" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["21"]["merge_sha"], "abc123")
        self.assertEqual(glab.updates, 1)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_the_size_limit_is_the_readers(self) -> None:
        self.assertEqual(walkthrough.MAX_REPORT_BYTES, proof_reports.MAX_REPORT_BYTES)
        self.capture("after")
        with mock.patch.object(walkthrough, "MAX_REPORT_BYTES", 10):
            out = self.build(FakeGlab())[1]
        self.assertIn("VISUAL_PROOF=missing", out)
        self.assertIsNone(self.stored())

    def test_an_unreadable_index_is_not_overwritten(self) -> None:
        self.capture("after")
        index = self.repo / ".proof" / "index.json"
        index.write_text("not json", encoding="utf-8")
        code, out = self.build(FakeGlab())
        self.assertEqual(code, 1, out)
        self.assertEqual(index.read_text(encoding="utf-8"), "not json")


class StateTests(ProofTestCase):
    def declared(self, *extra: str) -> tuple:
        glab = FakeGlab()
        code, out = self.build(glab, *extra)
        return code, glab.description.splitlines()[0], out

    def test_an_edit_after_the_capture_is_stale(self) -> None:
        self.capture("after")
        (self.repo / "app.txt").write_text("edited\n", encoding="utf-8")
        self.assertEqual(self.declared()[:2], (0, "VISUAL_PROOF=stale"))

    def test_layout_lint_errors_are_failing(self) -> None:
        manifest = self.tmp / "manifest.json"
        finding = {"severity": "error", "kind": "overlap", "element": "#a", "detail": "4px", "allowed": None}
        manifest.write_text(json.dumps({"entries": [{
            "state": "board.done", "viewport": "phone", "theme": "light", "screenshot": str(self.shot),
            "findings": [finding]}]}), encoding="utf-8")
        self.assertEqual(self.run_cli("capture-after", str(self.repo), "--manifest", str(manifest))[0], 0)
        self.assertEqual(self.declared()[:2], (0, "VISUAL_PROOF=failing"))

    def test_no_after_capture_is_missing_and_stores_nothing(self) -> None:
        self.capture("before")
        code, line, out = self.declared()
        self.assertEqual((code, line), (1, "VISUAL_PROOF=missing"), out)
        self.assertIsNone(self.stored())

    def test_a_tree_that_cannot_be_read_is_unknown_not_fresh(self) -> None:
        self.capture("after")
        with mock.patch.object(proof, "tree_of", return_value=(None, "git failed")):
            self.assertEqual(self.declared()[1], "VISUAL_PROOF=unknown")

    def test_no_ui_declares_not_applicable_and_writes_no_report(self) -> None:
        self.assertEqual(self.declared("--no-ui")[:2], (0, "VISUAL_PROOF=n/a"))
        self.assertFalse((self.repo / ".proof").exists())

    def test_tracked_proof_files_are_refused(self) -> None:
        (self.repo / ".proof").mkdir()
        (self.repo / ".proof" / "old.png").write_bytes(PNG)
        git(self.repo, "add", "-f", ".proof/old.png")
        code, out = self.run_cli("capture-after", str(self.repo), "--image", f"s:phone-light={self.shot}")
        self.assertEqual(code, 1)
        self.assertIn("tracked files", out)
        self.assertEqual(self.declared()[1], "VISUAL_PROOF=unknown")


class MrLineTests(unittest.TestCase):
    def test_there_is_one_line_at_the_top_and_the_rest_is_kept(self) -> None:
        body = "Closes #7\r\n\r\nVISUAL_PROOF=missing\r\n\r\nmore, and VISUAL_PROOF=failing in prose"
        once = mr_line.with_line(body, "fresh")
        self.assertEqual(mr_line.with_line(once, "fresh"), once)
        self.assertTrue(once.startswith("VISUAL_PROOF=fresh\n\nCloses #7"), once)
        self.assertNotIn("VISUAL_PROOF=missing", once)
        self.assertIn("more, and VISUAL_PROOF=failing in prose", once)

    def test_windows_line_endings_are_not_written_back_doubled(self) -> None:
        glab = FakeGlab("Closes #7\r\n\r\nmore\r\n")
        self.assertEqual(mr_line.set_line(Path("."), 21, "fresh", glab), (True, "MR_LINE=written"))
        self.assertEqual(glab.description, "VISUAL_PROOF=fresh\n\nCloses #7\n\nmore\n")

    def test_an_unreadable_description_is_never_written_over(self) -> None:
        def no_key(args: list) -> subprocess.CompletedProcess:
            self.assertEqual(args[:2], ["mr", "view"])
            return subprocess.CompletedProcess(args, 0, stdout='{"iid": 21}', stderr="")

        ok, line = mr_line.set_line(Path("."), 21, "fresh", no_key)
        self.assertEqual((ok, line.split()[0]), (False, "MR_LINE=failed"))

    def test_an_update_the_read_back_does_not_show_is_unknown(self) -> None:
        ok, line = mr_line.set_line(Path("."), 21, "fresh", FakeGlab(keeps_updates=False))
        self.assertEqual((ok, line.split()[0]), (False, "MR_LINE=unknown"))


if __name__ == "__main__":
    unittest.main()
