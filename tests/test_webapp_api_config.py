"""GET / POST /api/config — shape + allow-list + validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.boot_autostart as boot_autostart_mod


@pytest.fixture(autouse=True)
def _isolated_startup_dir(tmp_path: Path, monkeypatch) -> Path:
    """Point boot_autostart's Startup folder at a tmp dir for every test in
    this module — the real per-user Startup folder must never be touched
    by a test run."""
    startup_dir = tmp_path / "Startup"
    monkeypatch.setattr(boot_autostart_mod, "_startup_dir", lambda: startup_dir)
    return startup_dir


class TestGetConfig:
    def test_returns_expected_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "host" in body
        assert "port" in body
        assert "projects_dir" in body
        assert "projects_ignore" in body
        assert isinstance(body["projects_ignore"], list)
        assert "apps_scan_root" in body
        assert "team_os_dir" in body
        assert "terminal_history_lines" in body
        assert isinstance(body["terminal_history_lines"], int)
        assert "terminal_history_lines_min" in body
        assert "terminal_history_lines_max" in body
        assert (
            body["terminal_history_lines_min"]
            <= body["terminal_history_lines"]
            <= body["terminal_history_lines_max"]
        )
        assert "copilot" in body
        # auth_password_set is what the SPA shows in the login overlay
        # ("a password is required" vs not). Bool, not the password itself.
        assert isinstance(body["auth_password_set"], bool)
        assert body["auth_password_set"] is False  # default conftest config
        assert body["boot_autostart_enabled"] is False  # nothing enabled yet

    def test_copilot_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        cp = body["copilot"]
        assert set(cp) == {
            "skip_permissions", "model", "models_available",
            "autopilot", "context", "contexts_available",
            "effort", "efforts_available", "computed_flags",
        }
        assert isinstance(cp["skip_permissions"], bool)
        assert isinstance(cp["autopilot"], bool)
        # Config-driven list (lite Phase 3): the defaults ship three ids.
        assert cp["models_available"] == [
            "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"
        ]
        # Default config → the default model + autopilot/context/effort
        # compose into the launch line.
        assert cp["model"] == "gpt-5.6-luna"
        assert cp["computed_flags"] == (
            "--model gpt-5.6-luna --autopilot --context long_context "
            "--effort xhigh"
        )


class TestPatchConfig:
    def test_patches_allowed_field(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"copilot_effort": "low"}
        )
        assert resp.status_code == 200
        assert "--effort low" in resp.json()["copilot_flags"]
        # And the in-memory cfg was swapped.
        assert app.state.webapp_config.copilot_effort == "low"

    def test_patch_never_persists_session_host_port_env_override(
        self, webapp_client, monkeypatch
    ):
        """The e2e pre-ship gate's disposable webapp sets
        LAUNCHER_SESSION_HOST_PORT to point at a disposable session-host
        (issue #260) rather than the live :8466. A config PATCH against
        that disposable webapp must never bake the override into the
        real, shared config/webapp_config.json on save — only load it
        for the running process's own in-memory use. Discovered live: a
        Settings-tab e2e test triggered exactly this, overwriting the
        user's real session_host_port with a disposable autoboot port
        and taking the live session-host down until the file was
        hand-repaired (issue #435 follow-up)."""
        client, app, overrides = webapp_client
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert json.loads(cfg_path.read_text())["session_host_port"] == 8466

        monkeypatch.setenv("LAUNCHER_SESSION_HOST_PORT", "59999")
        resp = client.post("/api/config", json={"copilot_effort": "low"})
        assert resp.status_code == 200

        # The running process's own state sees the override — it must keep
        # talking to the disposable session-host for the rest of its life.
        assert app.state.webapp_config.session_host_port == 59999
        # But the shared on-disk file must be untouched.
        on_disk = json.loads(cfg_path.read_text())
        assert on_disk["session_host_port"] == 8466

    def test_projects_ignore_round_trips(self, webapp_client):
        """projects_ignore is a list field — the endpoint accepts it,
        strips blank entries, and persists it on the in-memory cfg."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config",
            json={"projects_ignore": ["archive", "  ", "*-old"]},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.projects_ignore == ["archive", "*-old"]
        # And it survives a GET round-trip.
        body = client.get("/api/config").json()
        assert body["projects_ignore"] == ["archive", "*-old"]

    def test_coding_hidden_agents_round_trips_and_persists(self, webapp_client):
        """coding_hidden_agents (issue #666) is a list field like
        projects_ignore: it patches through, strips blanks, surfaces on the
        next GET, and actually reaches disk — a hidden button must stay
        hidden across a webapp restart, not just in process state."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        # Default is empty — every button visible until the user hides one.
        assert client.get("/api/config").json()["coding_hidden_agents"] == []
        resp = client.post(
            "/api/config",
            json={"coding_hidden_agents": ["copilot", "  ", "github"]},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.coding_hidden_agents == [
            "copilot",
            "github",
        ]
        body = client.get("/api/config").json()
        assert body["coding_hidden_agents"] == ["copilot", "github"]
        cfg_path = overrides["tmp_webapp_cfg_path"]
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["coding_hidden_agents"] == ["copilot", "github"]
        assert load_webapp_config(cfg_path).coding_hidden_agents == [
            "copilot",
            "github",
        ]
        # Un-hiding clears it back out.
        client.post("/api/config", json={"coding_hidden_agents": []})
        assert client.get("/api/config").json()["coding_hidden_agents"] == []

    def test_terminal_history_lines_round_trips(self, webapp_client):
        """terminal_history_lines (issue #435 follow-up, Settings tab) is
        in the allow-list — it patches through, surfaces on the next GET,
        AND actually reaches disk (issue #441: it originally shipped
        missing from both load_webapp_config's constructor call and
        save_webapp_config's payload, so a Save was in-memory only and
        silently reverted on restart or on the next unrelated PATCH —
        which the old in-memory-only assertions here couldn't catch)."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        resp = client.post("/api/config", json={"terminal_history_lines": 5000})
        assert resp.status_code == 200
        assert app.state.webapp_config.terminal_history_lines == 5000
        body = client.get("/api/config").json()
        assert body["terminal_history_lines"] == 5000
        # Persisted, not just process state: the on-disk JSON carries it and
        # a fresh load (a webapp restart, or update_webapp_config's
        # reload-before-save on any later PATCH) round-trips it.
        cfg_path = overrides["tmp_webapp_cfg_path"]
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["terminal_history_lines"] == 5000
        assert load_webapp_config(cfg_path).terminal_history_lines == 5000

    def test_terminal_history_lines_rejects_out_of_range(self, webapp_client):
        client, _, _ = webapp_client
        too_low = client.post("/api/config", json={"terminal_history_lines": 1})
        assert too_low.status_code == 400
        too_high = client.post(
            "/api/config", json={"terminal_history_lines": 1_000_000}
        )
        assert too_high.status_code == 400

    def test_copilot_toggle_round_trips(self, webapp_client):
        """The Copilot launch toggle patches through and surfaces as the
        composed `copilot` flag on the next GET."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"copilot_skip_permissions": True}
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.copilot_skip_permissions is True
        cp = client.get("/api/config").json()["copilot"]
        assert cp["skip_permissions"] is True
        assert "--allow-all" in cp["computed_flags"]

    def test_copilot_model_round_trips(self, webapp_client):
        """A model from the config-driven `copilot_models` list patches
        through and surfaces as `--model` (plus the persisted `--effort`,
        legal now the model is explicit); an id outside the list falls back
        to '' (Copilot auto) with a logged warning — never a crash, never a
        silent launch of a tenant-gated id kept in config."""
        client, app, _ = webapp_client
        models = client.get("/api/config").json()["copilot"]["models_available"]
        assert models == ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        model = models[1]
        resp = client.post("/api/config", json={"copilot_model": model})
        assert resp.status_code == 200
        assert app.state.webapp_config.copilot_model == model
        cp = client.get("/api/config").json()["copilot"]
        assert cp["model"] == model
        assert f"--model {model}" in cp["computed_flags"]
        assert "--effort xhigh" in cp["computed_flags"]
        # An unknown model self-heals to '' (auto) instead of 400ing —
        # the validator falls back rather than crashing (lite Phase 3).
        resp = client.post(
            "/api/config", json={"copilot_model": "gpt-not-real"}
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.copilot_model == ""
        cp = client.get("/api/config").json()["copilot"]
        assert cp["model"] == ""
        assert "--model" not in cp["computed_flags"]

    def test_copilot_effort_omitted_without_explicit_model(self, webapp_client):
        """Empirically verified on Copilot CLI 1.0.70: the auto model rejects
        --effort, so effort must be silently omitted while copilot_model is
        '' — and reappear once an explicit model is set."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"copilot_model": ""})
        assert resp.status_code == 200
        cp = client.get("/api/config").json()["copilot"]
        assert cp["effort"] == "xhigh"          # persisted, but…
        assert "--effort" not in cp["computed_flags"]  # …not emitted
        client.post("/api/config", json={"copilot_model": "gpt-5.6-luna"})
        cp = client.get("/api/config").json()["copilot"]
        assert "--effort xhigh" in cp["computed_flags"]

    def test_copilot_autopilot_and_context_round_trip(self, webapp_client):
        """copilot_autopilot / copilot_context patch through and surface as
        --autopilot / --context; '' context omits the flag; an invalid
        context falls back to the default with a warning (never a 400)."""
        client, app, _ = webapp_client
        cp = client.get("/api/config").json()["copilot"]
        assert cp["autopilot"] is True
        assert "--autopilot" in cp["computed_flags"]
        assert "--context long_context" in cp["computed_flags"]
        resp = client.post(
            "/api/config",
            json={"copilot_autopilot": False, "copilot_context": ""},
        )
        assert resp.status_code == 200
        cp = client.get("/api/config").json()["copilot"]
        assert cp["autopilot"] is False
        assert "--autopilot" not in cp["computed_flags"]
        assert "--context" not in cp["computed_flags"]
        # Invalid context → validator falls back to the default.
        resp = client.post("/api/config", json={"copilot_context": "huge"})
        assert resp.status_code == 200
        assert app.state.webapp_config.copilot_context == "long_context"

    def test_copilot_models_not_postable(self, webapp_client):
        """copilot_models is read-only from the UI: exposed in GET as
        models_available, but a POST attempt is ignored (it's edited in
        webapp_config.json directly)."""
        client, app, _ = webapp_client
        before = list(app.state.webapp_config.copilot_models)
        resp = client.post(
            "/api/config", json={"copilot_models": ["evil-model"]}
        )
        assert resp.status_code == 200  # unknown keys are dropped, not errors
        assert app.state.webapp_config.copilot_models == before

    def test_ignores_unknown_field_silently(self, webapp_client):
        """The endpoint filters by allow-list — unknown keys are dropped,
        not error'd. Confirms the whitelist isn't accidentally loosened."""
        client, app, _ = webapp_client
        before = app.state.webapp_config.copilot_model
        resp = client.post(
            "/api/config",
            json={"auth_token": "should-be-ignored", "copilot_model": before},
        )
        assert resp.status_code == 200
        # auth_token is NOT in the allow-list and must not be patched here.
        assert app.state.webapp_config.auth_token == ""


class TestBootAutostart:
    """POST /api/settings/boot-autostart — issue #456 part 1/2."""

    def test_enable_then_get_config_reflects_it(
        self, webapp_client, _isolated_startup_dir
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/settings/boot-autostart", json={"enabled": True}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "boot_autostart_enabled": True}
        assert client.get("/api/config").json()["boot_autostart_enabled"] is True
        assert (
            _isolated_startup_dir / boot_autostart_mod.STARTUP_BAT_NAME
        ).is_file()

    def test_disable_removes_it(self, webapp_client, _isolated_startup_dir):
        client, _, _ = webapp_client
        client.post("/api/settings/boot-autostart", json={"enabled": True})
        resp = client.post(
            "/api/settings/boot-autostart", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "boot_autostart_enabled": False}
        assert client.get("/api/config").json()["boot_autostart_enabled"] is False
        assert not (
            _isolated_startup_dir / boot_autostart_mod.STARTUP_BAT_NAME
        ).exists()

    def test_disable_when_never_enabled_is_a_noop(
        self, webapp_client, _isolated_startup_dir
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/settings/boot-autostart", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "boot_autostart_enabled": False}
