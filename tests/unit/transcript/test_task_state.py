"""A session background task's state, server and browser copy.

``src/session_view/task_state.py`` declares the phases and the one
transition rule the Chat view's background-task read uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.session_view.task_state import COMPLETED, TaskPhase, next_status, task_phase

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_a_finished_task_never_reopens_or_changes_status() -> None:
    """The first reported status is final; a status-less event leaves a task running.

    state-machine-proof: background-task-state
    """
    assert next_status(None, "") is None
    assert next_status(None, "failed") == "failed"
    assert next_status(None, "Not A Word!") == TaskPhase.FINISHED
    for later in ("", "completed", "running", "killed"):
        assert next_status("failed", later) == "failed"
    assert task_phase(None) is TaskPhase.RUNNING
    assert task_phase(TaskPhase.RUNNING.value) is TaskPhase.RUNNING
    for ended in ("failed", COMPLETED, TaskPhase.FINISHED.value):
        assert task_phase(ended) is TaskPhase.FINISHED
    with pytest.raises(ValueError):
        TaskPhase("completed")

