"""The glab cache is module-global; every Board test starts and ends with it empty."""

from __future__ import annotations

import pytest

from src import gitlab_client


@pytest.fixture(autouse=True)
def _pristine_glab_cache():
    """The glab cache is module-global — every test starts and ends empty."""
    gitlab_client.reset_cache()
    yield
    gitlab_client.reset_cache()
