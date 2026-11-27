"""The command a model switch types, and what it refuses to type."""

from __future__ import annotations

import pytest

from src.session_view import model_switch as ms


def test_copilot_switches_to_a_listed_id_by_its_own_command():
    offered = ["gpt-6-luna", "mai-code-2-flash"]
    assert ms.switch_keys("copilot", "mai-code-2-flash", offered) == [
        ("/model mai-code-2-flash", True)]


@pytest.mark.parametrize("agent", ["other", ""])
def test_an_agent_with_no_in_session_switch_is_refused(agent):
    with pytest.raises(ms.SwitchRefused):
        ms.switch_keys(agent, "gpt-6-luna", ["gpt-6-luna"])


@pytest.mark.parametrize("value,offered", [
    ("opus", ["gpt-6-luna"]),                            # not on the install's list
    ("gpt-6-luna\r/clear", ["gpt-6-luna\r/clear"]),      # listed, but types two commands
    ("a b", ["a b"]),
    (None, ["gpt-6-luna"]),
])
def test_copilot_types_nothing_outside_its_list_or_id_shape(value, offered):
    with pytest.raises(ValueError):
        ms.switch_keys("copilot", value, offered)
