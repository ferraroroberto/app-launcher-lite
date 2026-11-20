"""The launch layer and the saved config carry only the agents this fork launches.

``src/agents.py`` ``AGENTS`` refuses every id but Copilot, yet a sync from
app-launcher keeps bringing back per-agent builders and settings that nothing
can reach, because each is still referenced from a table. This holds the two
places they land: ``src/launching/flags.py`` and ``WebappConfig``.
"""
from __future__ import annotations

import re
from dataclasses import fields

from src import agents
from src.launching import flags
from src.webapp_config import WebappConfig

# Agent ids the full app launches and this fork does not.
_RETIRED_AGENTS = ("claude", "codex", "grok", "pi", "antigravity")

# A `claude_*` field that is not a launch setting of the Claude agent.
_NOT_AN_AGENT_SETTING = {"claude_show_local_window"}


def test_every_flag_builder_belongs_to_a_launchable_agent() -> None:
    builders = {
        match.group(1)
        for name in dir(flags)
        if (match := re.fullmatch(r"build_(\w+)_flags", name)) and match.group(1) != "resume"
    }
    assert builders == {"copilot"}
    assert builders <= set(agents.AGENTS)


def test_the_config_holds_no_setting_for_a_retired_agent() -> None:
    stale = [
        spec.name
        for spec in fields(WebappConfig)
        if spec.name.split("_", 1)[0] in _RETIRED_AGENTS
        and spec.name not in _NOT_AN_AGENT_SETTING
    ]
    assert not stale, f"settings for an agent this fork cannot launch: {stale}"
