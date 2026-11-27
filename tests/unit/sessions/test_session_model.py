"""The session model id: derived from the logged model and the spawn flags,
and never guessed."""

from __future__ import annotations

import pytest

from src.session_model import attach_models, session_model_label


@pytest.mark.parametrize("session,expected", [
    # The model Copilot logged follows an in-session /model.
    ({"agent": "copilot", "flags": "--model gpt-6-luna", "transcript_model": "mai-code-2-flash"},
     "mai-code-2-flash"),
    ({"agent": "copilot", "flags": "--model gpt-6-luna"}, "gpt-6-luna"),
    # No flag = the agent's default model: nothing to show, not a guess.
    ({"agent": "copilot", "flags": ""}, None),
    # Any other agent has no model to name.
    ({"agent": "other", "flags": "--model gpt-6-luna"}, None),
])
def test_session_model_label(session, expected):
    assert session_model_label(session) == expected


def test_attach_models_returns_new_dicts():
    live = [{"agent": "copilot", "flags": "--model gpt-6-luna"}]
    out = attach_models(live)
    assert out == [{"agent": "copilot", "flags": "--model gpt-6-luna", "model": "gpt-6-luna"}]
    assert "model" not in live[0]
