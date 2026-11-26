"""Every reader of Copilot's own files follows ``COPILOT_HOME``, and none spells ``~/.copilot`` itself.

The state-folder resolvers are pinned in ``test_state_location.py``. This pins the readers of the
rest of the Copilot home (usage store, sign-in config, settings, session logs) so a machine that
sets ``COPILOT_HOME`` is read where Copilot wrote, not from an empty ``~/.copilot``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from src import copilot_usage

_ROOT = Path(__file__).resolve().parents[3]
_COPILOT = _ROOT / "copilot"
# The one place each tier (and the installer) spells the default; every reader goes through these.
_HELPERS = {
    "src/chief/copilot_state.py",
    "copilot/hooks/_lib.py",
    "copilot/skills/_lib/fleet_core/hooks_state.py",
    "copilot/install.py",
}
_LITERAL = re.compile(r"""["']\.copilot["']""")


def _fresh(name: str, path: Path) -> ModuleType:
    """A private copy of a module, so its import-time constants are read under the test's environment."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.syspath_prepend(str(_COPILOT / "skills" / "_lib"))
    return tmp_path


def _readers() -> dict[str, Path]:
    """Where each reader looks under the current environment."""
    return {
        "usage store": copilot_usage.default_db_path(),
        "usage sign-in config": copilot_usage.default_config_path(),
        # A private copy: tests/conftest.py stubs the shared module's settings_path for every test.
        "remote settings": _fresh("lite_copilot_remote", _ROOT / "src" / "launching" / "copilot_remote.py").settings_path(),
        "transcript lookup": _fresh("lite_transcript_locate", _ROOT / "src" / "session_view" / "transcript_locate.py")._COPILOT_STATE_DIR,
        "web link host": _fresh("lite_copilot_host", _ROOT / "src" / "session_view" / "copilot_host.py")._COPILOT_CONFIG,
        "chief context": _fresh("lite_copilot_context", _COPILOT / "skills" / "_lib" / "chief" / "copilot_context.py").COPILOT_SESSION_STATE,
        "probe": _fresh("lite_copilot_probe", _COPILOT / "scripts" / "copilot_probe.py").SESSION_STATE_DIR,
    }


def test_copilot_home_moves_every_reader(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("COPILOT_HOME", str(elsewhere))
    for name, path in _readers().items():
        assert elsewhere in path.parents, f"{name} reads {path}, not under COPILOT_HOME"


def test_without_copilot_home_every_reader_keeps_the_default(home: Path) -> None:
    default = home / ".copilot"
    for name, path in _readers().items():
        assert default in path.parents, f"{name} reads {path}, not under ~/.copilot"


def test_no_reader_spells_the_default_home_itself() -> None:
    offenders = []
    for folder in ("src", "app", "copilot", "scripts"):
        for path in (_ROOT / folder).rglob("*.py"):
            rel = path.relative_to(_ROOT).as_posix()
            if rel in _HELPERS or "tests" in path.relative_to(_ROOT / folder).parts:
                continue
            if _LITERAL.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
    assert not offenders, f"build the Copilot home through the tier's copilot_home(): {offenders}"
