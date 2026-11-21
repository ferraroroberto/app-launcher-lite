"""Hermetic tests for skills/_lib/runtime_files.py (stdlib unittest, no deps).

Run:  python -m unittest discover -s tests -v

Covers the guard: a path outside the repo's code folders is written, a path under
one is refused before any byte or folder is created.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "skills" / "_lib"
sys.path.insert(0, str(LIB_DIR))

import runtime_files as rf  # noqa: E402


class GuardedWriterTests(unittest.TestCase):
    def test_writes_outside_the_code_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "out.txt"
            rf.make_dirs(target.parent)
            rf.save_text(target, "héllo\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "héllo\n")
            rf.save_bytes(target, b"\x00\x01")
            self.assertEqual(target.read_bytes(), b"\x00\x01")

    def test_refuses_each_code_folder(self):
        for name in rf.CODE_FOLDERS:
            with self.subTest(folder=name):
                with self.assertRaises(ValueError):
                    rf.checked(rf.REPO_ROOT / name / "x.txt")
                with self.assertRaises(ValueError):
                    rf.checked(rf.REPO_ROOT / name)

    def test_refusal_creates_nothing(self):
        folder = rf.REPO_ROOT / "skills" / "_never_created"
        with self.assertRaises(ValueError):
            rf.make_dirs(folder)
        with self.assertRaises(ValueError):
            rf.save_text(folder / "f.txt", "x")
        self.assertFalse(folder.exists())

    def test_refuses_dotdot_back_into_a_code_folder(self):
        sneaky = rf.REPO_ROOT / "docs" / ".." / "hooks" / "x.txt"
        with self.assertRaises(ValueError):
            rf.checked(sneaky)

    def test_non_code_folder_inside_the_repo_is_allowed(self):
        self.assertEqual(rf.checked(rf.REPO_ROOT / "docs" / "x.md"), (rf.REPO_ROOT / "docs" / "x.md").resolve())


if __name__ == "__main__":
    unittest.main()
