"""The launch layer, the saved config and the transcript reader carry only the agents this fork launches.

``src/agents.py`` ``AGENTS`` refuses every id but Copilot, yet a sync from
app-launcher keeps bringing back per-agent builders, settings and transcript
grammars that nothing can reach, because each is still referenced from a
table. This holds the places they land: ``src/launching/flags.py``,
``WebappConfig`` and the transcript reader's agent and grammar tables.
"""
from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from src import agents
from src.launching import flags
from src.session_view.transcript import FLAVORS
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


def test_the_transcript_reader_maps_only_launchable_agents() -> None:
    from app.webapp.routers.session_transcript import _FLAVOR_BY_AGENT

    assert set(_FLAVOR_BY_AGENT) <= set(agents.AGENTS)
    assert set(_FLAVOR_BY_AGENT.values()) == set(FLAVORS)


def test_there_is_no_transcript_grammar_for_a_retired_agent() -> None:
    grammars = {path.stem for path in Path(flags.__file__).parents[1].glob("transcript_flavors/*.py")}
    assert not grammars & set(_RETIRED_AGENTS), grammars & set(_RETIRED_AGENTS)
