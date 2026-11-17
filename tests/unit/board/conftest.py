"""Board unit tests start each test with the module-global glab cache and failure latches reset."""

from __future__ import annotations

import pytest

from app.webapp.routers import board_spawn
from src import board
from src.clients import gitlab as forge


@pytest.fixture(autouse=True)
def _pristine_gh_cache():
    """The glab cache is module-global — every test starts and ends empty."""
    forge.reset_cache()
    yield
    forge.reset_cache()


@pytest.fixture(autouse=True)
def _pristine_failure_latches(monkeypatch):
    """The once-per-failure log latches (#915) are module-global too."""
    monkeypatch.setattr(board_spawn, "_session_host_down", False, raising=False)
    monkeypatch.setattr(board, "_UNREADABLE_JOBS", set(), raising=False)
