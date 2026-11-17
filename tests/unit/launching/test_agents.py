"""src.agents — the Copilot-only coding-agent registry + PATH detection."""

from __future__ import annotations

import pytest

from src import agents


class TestRegistry:
    def test_copilot_is_the_only_coding_agent(self):
        assert set(agents.AGENTS) == {"copilot"}
        assert agents.DEFAULT_AGENT == "copilot"

    def test_ssh_is_a_session_host_agent_only(self):
        assert set(agents.SESSION_HOST_AGENTS) == {"copilot", "ssh"}


class TestCommandFor:
    def test_copilot_resolves(self):
        assert agents.command_for("copilot") == "copilot"

    def test_ssh_resolves_for_loopback_session_host(self):
        assert agents.command_for("ssh") == "ssh"

    @pytest.mark.parametrize("agent", ["claude", "codex", "bogus"])
    def test_unknown_agent_raises(self, agent: str):
        with pytest.raises(ValueError):
            agents.command_for(agent)


class TestQuitCommandFor:
    def test_copilot_quits_with_slash_exit(self):
        assert agents.quit_command_for("copilot") == "/exit"

    def test_ssh_quits_with_exit(self):
        assert agents.quit_command_for("ssh") == "exit"

    def test_unknown_agent_falls_back_to_default(self):
        # A bad id must never block a stop — fall back, don't raise.
        assert agents.quit_command_for("bogus") == "/exit"


class TestResumeCommandFor:
    def test_copilot_resumes_with_flag(self):
        assert agents.resume_command_for("copilot") == "--resume"

    def test_ssh_has_no_resume_path(self):
        assert agents.resume_command_for("ssh") == ""

    def test_unknown_agent_returns_empty(self):
        # A bad id is "not resumable", never a raise.
        assert agents.resume_command_for("bogus") == ""


class TestNativeSessionNameFlags:
    def test_copilot_receives_spawn_time_name(self):
        assert (
            agents.native_session_name_flags_for("copilot", "Issue 556: sync picker")
            == '--name "Issue 556: sync picker"'
        )

    @pytest.mark.parametrize("agent", ["ssh", "claude", "bogus"])
    def test_other_agents_skip_native_name(self, agent: str):
        assert agents.native_session_name_flags_for(agent, "Issue 556") == ""

    def test_shell_unsafe_title_skips_native_name(self):
        assert agents.native_session_name_flags_for("copilot", "name & command") == ""

    def test_native_name_matches_launcher_title_cap(self):
        expected = "x" * 60
        assert (
            agents.native_session_name_flags_for("copilot", "x" * 70)
            == f'--name "{expected}"'
        )


class TestIsInstalled:
    def test_true_when_command_on_path(self, monkeypatch):
        # `path=`: is_installed resolves against the effective PATH, not
        # implicitly against the process environment.
        monkeypatch.setattr(
            agents.shutil, "which", lambda cmd, path=None: f"C:\\bin\\{cmd}"
        )
        assert agents.is_installed("copilot") is True

    def test_false_when_command_missing(self, monkeypatch):
        monkeypatch.setattr(
            agents.shutil, "which", lambda cmd, path=None: None
        )
        assert agents.is_installed("copilot") is False

    def test_false_for_unknown_agent(self):
        assert agents.is_installed("bogus") is False


class TestIsFullscreen:
    def test_copilot_is_fullscreen(self):
        # A differential TUI: skips raw-ring replay and gets a forced repaint.
        assert agents.is_fullscreen("copilot") is True

    def test_ssh_is_inline(self):
        assert agents.is_fullscreen("ssh") is False

    def test_unknown_agent_defaults_to_inline(self):
        # Unknown id → safe inline default, never raises.
        assert agents.is_fullscreen("bogus") is False


class TestDetectAgents:
    def test_shape_and_keys(self, monkeypatch):
        monkeypatch.setattr(
            agents.shutil, "which", lambda cmd, path=None: None
        )
        detected = agents.detect_agents()
        # One entry per Coding-tab agent, each with the SPA-facing keys.
        assert {d["id"] for d in detected} == set(agents.AGENTS)
        for d in detected:
            assert set(d) == {"id", "label", "available", "fullscreen", "console_input"}
            assert isinstance(d["available"], bool)
            assert isinstance(d["fullscreen"], bool)
            assert isinstance(d["console_input"], bool)
        by_id = {d["id"]: d["fullscreen"] for d in detected}
        assert by_id["copilot"] is True
        assert "ssh" not in by_id  # service integration, not a Coding-tab button

    def test_console_input_flags_match_the_recorded_probes(self, monkeypatch):
        monkeypatch.setattr(agents.shutil, "which", lambda cmd, path=None: None)
        by_id = {d["id"]: d["console_input"] for d in agents.detect_agents()}
        assert by_id == {"copilot": True}
        assert agents.supports_console_input("copilot") is True
        # ssh is a service integration, deliberately never probed.
        assert agents.supports_console_input("ssh") is False
        assert agents.supports_console_input("no-such-agent") is False

    def test_availability_reflects_path(self, monkeypatch):
        monkeypatch.setattr(
            agents.shutil,
            "which",
            lambda cmd, path=None: "C:\\bin\\copilot" if cmd == "copilot" else None,
        )
        by_id = {d["id"]: d["available"] for d in agents.detect_agents()}
        assert by_id["copilot"] is True
        monkeypatch.setattr(agents.shutil, "which", lambda cmd, path=None: None)
        by_id = {d["id"]: d["available"] for d in agents.detect_agents()}
        assert by_id["copilot"] is False
