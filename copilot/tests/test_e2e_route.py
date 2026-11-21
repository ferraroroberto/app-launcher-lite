"""Hermetic tests for skills/e2e/e2e_route.py (stdlib unittest, no deps).

Run:  python -m unittest discover -s tests -v

Synthetic repos in tempdirs exercise the probe facts, the byte-verbatim
bootstrap contract (including refuse-on-divergence), and the route fail-safe.
One integration case runs the real bundled classify_e2e.py with an explicit
file list (no git needed) and asserts the fail-safe `full` on a repo with no
`[e2e]` table.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "e2e"
sys.path.insert(0, str(SKILL_DIR))

import e2e_route as er  # noqa: E402


def _capture(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args)
    return rc, buf.getvalue()


class E2eRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.source = self.root / "bundled" / "classify_e2e.py"
        self.source.parent.mkdir()
        self.source.write_text(
            "print('E2E_TIER=full')\nprint('E2E_REASON=stub')\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(exist_ok=True)
        return repo

    # ---- probe facts ----

    def test_bare_repo_probes_all_absent(self):
        repo = self._repo("bare")
        self.assertEqual(er.classifier_state(repo, self.source), ("absent", "n/a"))
        self.assertEqual(er.e2e_table_state(repo), "absent")
        self.assertEqual(er.suite_state(repo), "absent")
        self.assertEqual(er.detect_web_surface(repo)[0], "no")

    def test_declared_web_layer_wins(self):
        repo = self._repo("web")
        (repo / ".fleet.toml").write_text('layer = "working-web"\n[e2e]\nx = 1\n',
                                          encoding="utf-8")
        self.assertEqual(er.detect_web_surface(repo)[:2], ("yes", "webapp"))
        self.assertEqual(er.e2e_table_state(repo), "present")

    def test_streamlit_dependency_and_invalid_toml(self):
        repo = self._repo("st")
        (repo / "requirements.txt").write_text("streamlit==1.30\n", encoding="utf-8")
        (repo / ".fleet.toml").write_text("layer = \n", encoding="utf-8")
        self.assertEqual(er.detect_web_surface(repo)[:2], ("yes", "streamlit"))
        self.assertEqual(er.e2e_table_state(repo), "invalid")

    def test_suite_needs_a_real_test_module(self):
        repo = self._repo("api")
        (repo / "tests" / "e2e").mkdir(parents=True)
        self.assertEqual(er.suite_state(repo), "absent")
        (repo / "tests" / "e2e" / "test_smoke.py").write_text(
            "def test_up(): pass\n", encoding="utf-8")
        self.assertEqual(er.suite_state(repo), "present")

    # ---- bootstrap contract ----

    def test_bootstrap_copies_verbatim_then_noop(self):
        repo = self._repo("boot")
        rc, out = _capture(er.cmd_bootstrap, repo, self.source, False)
        self.assertEqual(rc, 0)
        self.assertIn("BOOTSTRAP=copied", out)
        self.assertTrue(er.files_identical(self.source, repo / "scripts" / "classify_e2e.py"))
        rc, out = _capture(er.cmd_bootstrap, repo, self.source, False)
        self.assertEqual(rc, 0)
        self.assertIn("BOOTSTRAP=exists-identical", out)

    def test_bootstrap_refuses_diverged_custom_without_force(self):
        repo = self._repo("custom")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "classify_e2e.py").write_text("# legacy\n", encoding="utf-8")
        rc, out = _capture(er.cmd_bootstrap, repo, self.source, False)
        self.assertEqual(rc, 1)
        self.assertIn("BOOTSTRAP=refused", out)
        rc, out = _capture(er.cmd_bootstrap, repo, self.source, True)
        self.assertEqual(rc, 0)
        self.assertIn("BOOTSTRAP=copied", out)

    def test_bundled_default_points_inside_skill_folder(self):
        self.assertEqual(er.BUNDLED_CLASSIFIER.parent, SKILL_DIR)
        self.assertTrue(er.BUNDLED_CLASSIFIER.is_file())

    # ---- route fail-safe ----

    def test_route_without_classifier_reports_judgment(self):
        repo = self._repo("nojudge")
        rc, out = _capture(er.cmd_route, repo, [])
        self.assertEqual(rc, 0)
        self.assertIn("SOURCE=judgment", out)
        self.assertIn("E2E_TIER=unknown", out)

    def test_route_broken_classifier_escalates_to_full(self):
        repo = self._repo("broken")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "classify_e2e.py").write_text("raise SystemExit(3)\n",
                                                          encoding="utf-8")
        rc, out = _capture(er.cmd_route, repo, [])
        self.assertEqual(rc, 0)
        self.assertIn("SOURCE=classifier-error", out)
        self.assertIn("E2E_TIER=full", out)

    def test_route_passes_stub_classifier_through(self):
        repo = self._repo("stubbed")
        _capture(er.cmd_bootstrap, repo, self.source, False)
        rc, out = _capture(er.cmd_route, repo, [])
        self.assertEqual(rc, 0)
        self.assertIn("SOURCE=classifier", out)
        self.assertIn("E2E_TIER=full", out)

    def test_real_bundled_classifier_fails_safe_without_table(self):
        repo = self._repo("realboot")
        rc, _ = _capture(er.cmd_bootstrap, repo, er.BUNDLED_CLASSIFIER, False)
        self.assertEqual(rc, 0)
        rc, out = _capture(er.cmd_route, repo, ["src/x.py"])
        self.assertEqual(rc, 0)
        self.assertIn("SOURCE=classifier", out)
        self.assertIn("E2E_TIER=full", out)


if __name__ == "__main__":
    unittest.main()
