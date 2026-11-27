"""Guard: the tolerant JSON reader has one owner, ``src._json_io.read_json_or``."""

from __future__ import annotations

import re
from pathlib import Path

from src._json_io import read_json_or

_ROOT = Path(__file__).resolve().parents[3]
# The reader itself, plus readers whose "missing" and "unreadable" are different answers
# (installer_declaration.py is also vendored and stdlib-only, so it cannot import the reader).
_OK = {"src/_json_io.py", "src/chief_session_end.py", "src/desktop/lid_keep_awake.py", "src/launching/copilot_remote.py",
       "src/installer_declaration.py"}
_HAND_ROLLED = re.compile(r"json\.loads\([^\n]*\.read_text\([^\n]*\n(?:[^\n]*\n){0,1}\s*except FileNotFoundError")


def test_tolerant_json_reads_go_through_read_json_or() -> None:
    offenders = [
        rel for base in ("src", "app") for path in sorted((_ROOT / base).rglob("*.py"))
        if (rel := path.relative_to(_ROOT).as_posix()) not in _OK and _HAND_ROLLED.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, "a hand-rolled 'read JSON, missing is the default' block; use read_json_or:\n  " + "\n  ".join(offenders)


def test_read_json_or_separates_missing_corrupt_and_wrong_shape(tmp_path, caplog) -> None:
    assert read_json_or(tmp_path / "none.json", {"d": 1}, "x") == {"d": 1} and not caplog.records
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    bad.write_text("{nope", encoding="utf-8")
    assert read_json_or(good, {}, "x", expect=dict) == {"a": 1}
    assert read_json_or(good, [], "x", expect=list) == []
    assert read_json_or(bad, {}, "thing", expect=dict) == {} and "thing unreadable" in caplog.text
