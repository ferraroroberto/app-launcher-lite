"""POST /api/config: allow-list and validation."""

from __future__ import annotations

import json

import pytest

from src.webapp_config import update_webapp_config


class TestPatchConfig:
    def test_patches_allowed_field(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"copilot_effort": "low"}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "--effort low" in resp.json()["copilot"]["computed_flags"]
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

    def test_rejects_invalid_value_with_400(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/config", json={"copilot_context": "definitely-not-a-real-context"}
        )
        assert resp.status_code == 400


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
            json={"coding_hidden_agents": ["antigravity", "  ", "github"]},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.coding_hidden_agents == [
            "antigravity",
            "github",
        ]
        body = client.get("/api/config").json()
        assert body["coding_hidden_agents"] == ["antigravity", "github"]
        cfg_path = overrides["tmp_webapp_cfg_path"]
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["coding_hidden_agents"] == ["antigravity", "github"]
        assert load_webapp_config(cfg_path).coding_hidden_agents == [
            "antigravity",
            "github",
        ]
        # Un-hiding clears it back out.
        client.post("/api/config", json={"coding_hidden_agents": []})
        assert client.get("/api/config").json()["coding_hidden_agents"] == []

    def test_coding_favorite_agent_round_trips_and_rejects_unknown_ids(
        self, webapp_client
    ):
        """coding_favorite_agent (#1070) names the one agent whose launch
        button stays on a Coding project row.

        Unlike the list fields beside it this one is *validated*, not just
        coerced to a clean string: the row renders exactly one launch button
        from it, so an id that names no registered agent would leave the row
        with no way to launch anything. An unknown id — a typo, a stale
        client, an agent dropped from the registry — falls back to
        DEFAULT_AGENT rather than being stored.
        """
        from src.agents import DEFAULT_AGENT
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        # Defaults to the registry's own default agent, so a fresh config
        # already renders a working row.
        assert client.get("/api/config").json()["coding_favorite_agent"] == DEFAULT_AGENT

        resp = client.post("/api/config", json={"coding_favorite_agent": "copilot"})
        assert resp.status_code == 200
        assert app.state.webapp_config.coding_favorite_agent == "copilot"
        assert client.get("/api/config").json()["coding_favorite_agent"] == "copilot"
        # Reaches disk — the favourite must survive a webapp restart.
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).coding_favorite_agent == "copilot"

        # An unknown id is refused into the default, never stored.
        for junk in ("not-an-agent", "codex", "claude", "", "   ", None):
            resp = client.post("/api/config", json={"coding_favorite_agent": junk})
            assert resp.status_code == 200, junk
            assert app.state.webapp_config.coding_favorite_agent == DEFAULT_AGENT, junk


    @pytest.mark.parametrize(
        "key, default", [("open_sessions_in_window", False), ("open_sessions_in_chat", True)]
    )
    def test_open_sessions_switches_default_and_round_trip(
        self, webapp_client, key, default
    ):
        """The two Settings switches that choose where the SPA opens a session
        (1955): window off, Chat on by default; each reaches disk (a switch that
        reverts on restart would reopen the PC window the owner turned off) and
        flipping one leaves the other alone."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        other = (
            "open_sessions_in_chat" if key == "open_sessions_in_window"
            else "open_sessions_in_window"
        )
        body = client.get("/api/config").json()
        assert body[key] is default
        other_before = body[other]

        for value in (not default, default):
            resp = client.post("/api/config", json={key: value})
            assert resp.status_code == 200
            assert getattr(app.state.webapp_config, key) is value
            assert client.get("/api/config").json()[key] is value
            assert getattr(load_webapp_config(overrides["tmp_webapp_cfg_path"]), key) is value
            assert client.get("/api/config").json()[other] is other_before

    @pytest.mark.parametrize("key", ["open_sessions_in_window", "open_sessions_in_chat"])
    @pytest.mark.parametrize("junk", ["false", "no", 0, 1, None, []])
    def test_open_sessions_switches_reject_non_booleans(self, webapp_client, key, junk):
        """"false" is truthy in Python: a string must never flip either switch."""
        client, app, _ = webapp_client
        before = getattr(app.state.webapp_config, key)
        resp = client.post("/api/config", json={key: junk})
        assert resp.status_code == 400
        assert getattr(app.state.webapp_config, key) is before


    def test_chief_doorbell_defaults_off_and_round_trips(self, webapp_client):
        """chief_doorbell (#1645): types into the live chief, so default OFF.
        The save swaps app.state.webapp_config, which the tick re-reads every
        cycle, and reaches disk so it survives a restart."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["chief_doorbell"] is False

        resp = client.post("/api/config", json={"chief_doorbell": True})
        assert resp.status_code == 200
        assert app.state.webapp_config.chief_doorbell is True
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).chief_doorbell is True

        resp = client.post("/api/config", json={"chief_doorbell": False})
        assert resp.status_code == 200
        assert load_webapp_config(cfg_path).chief_doorbell is False

    @pytest.mark.parametrize("key", ["chief_job_failures"])
    def test_chief_failure_feeds_default_on_and_round_trip(self, webapp_client, key):
        """The job-failure feed: the swap on app.state is what the next failed
        run reads, and the save reaches disk."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()[key] is True

        resp = client.post("/api/config", json={key: False})
        assert resp.status_code == 200
        assert getattr(app.state.webapp_config, key) is False
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert getattr(load_webapp_config(cfg_path), key) is False
        assert client.get("/api/config").json()[key] is False

    @pytest.mark.parametrize("key", ["chief_job_self_heal", "chief_weekly_proposals"])
    def test_new_chief_automations_default_off_and_round_trip(self, webapp_client, key):
        """The two Automations switches fleet-config reads: off until the
        owner opts in, and the save reaches disk."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()[key] is False

        assert client.post("/api/config", json={key: True}).status_code == 200
        assert getattr(app.state.webapp_config, key) is True
        assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).__dict__[key] is True
        assert client.get("/api/config").json()[key] is True

        assert client.post("/api/config", json={key: False}).status_code == 200
        assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).__dict__[key] is False

    @pytest.mark.parametrize("key", ["chief_job_self_heal", "chief_weekly_proposals"])
    @pytest.mark.parametrize("junk", ["true", "false", 0, 1, None])
    def test_new_chief_automations_reject_non_booleans(self, webapp_client, key, junk):
        client, app, _ = webapp_client
        assert client.post("/api/config", json={key: junk}).status_code == 400
        assert getattr(app.state.webapp_config, key) is False

    def test_chief_fallback_minutes_defaults_to_30_and_round_trips(self, webapp_client):
        """The fallback wake's window: 30 until changed, 0 turns it off, and
        the chief reads the saved value from its settings block."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        body = client.get("/api/config").json()
        assert body["chief_fallback_minutes"] == 30
        assert body["chief_fallback_minutes_max"] == 1440
        for value in (0, 45):
            assert client.post("/api/config", json={"chief_fallback_minutes": value}).status_code == 200
            assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).chief_fallback_minutes == value
        assert app.state.webapp_config.chief_fallback_minutes == 45

    @pytest.mark.parametrize("junk", [True, 1.5, "30", -1, 1441])
    def test_chief_fallback_minutes_rejects_junk(self, webapp_client, junk):
        client, app, _ = webapp_client
        assert client.post("/api/config", json={"chief_fallback_minutes": junk}).status_code == 400
        assert app.state.webapp_config.chief_fallback_minutes == 30

    def test_chief_timings_default_and_round_trip(self, webapp_client):
        """Each tunable timing: today's value until edited, its range in the
        payload and saved by Settings; the fixed panel rides along."""
        from src.chief import timings
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        body = client.get("/api/config").json()
        for spec in timings.TUNABLES:
            assert body[spec.key] == spec.default
            assert (body[f"{spec.key}_min"], body[f"{spec.key}_max"]) == (spec.minimum, spec.maximum)
            assert client.post("/api/config", json={spec.key: spec.maximum}).status_code == 200
            assert getattr(load_webapp_config(overrides["tmp_webapp_cfg_path"]), spec.key) == spec.maximum
        assert body["chief_fixed_timings"]["values"] and body["chief_fixed_timings"]["rules"]

    @pytest.mark.parametrize("junk", [True, 1.5, "60", 0, 29, 601])
    def test_chief_timings_reject_junk(self, webapp_client, junk):
        client, app, _ = webapp_client
        assert client.post("/api/config", json={"chief_rering_seconds": junk}).status_code == 400
        assert app.state.webapp_config.chief_rering_seconds == 60


    def test_chief_auto_drain_defaults_on_and_round_trips(self, webapp_client):
        """The queue drain is on until the owner turns it off, and the save
        reaches disk so a restart keeps it."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["chief_auto_drain"] is True

        resp = client.post("/api/config", json={"chief_auto_drain": False})
        assert resp.status_code == 200
        assert app.state.webapp_config.chief_auto_drain is False
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).chief_auto_drain is False
        assert client.get("/api/config").json()["chief_auto_drain"] is False

    @pytest.mark.parametrize("junk", ["false", 0, None])
    def test_chief_auto_drain_rejects_non_booleans(self, webapp_client, junk):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_auto_drain": junk})
        assert resp.status_code == 400
        assert app.state.webapp_config.chief_auto_drain is True


    @pytest.mark.parametrize("key", ["chief_job_failures"])
    @pytest.mark.parametrize("junk", ["false", 0, None])
    def test_chief_failure_feeds_reject_non_booleans(self, webapp_client, key, junk):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={key: junk})
        assert resp.status_code == 400
        assert getattr(app.state.webapp_config, key) is True

    @pytest.mark.parametrize("junk", ["true", "yes", 0, 1, None, []])
    def test_chief_doorbell_rejects_non_booleans(self, webapp_client, junk):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_doorbell": junk})
        assert resp.status_code == 400
        assert app.state.webapp_config.chief_doorbell is False


    def test_config_reports_chief_model_and_worker_cap_read_only(self, webapp_client):
        """Settings › Chief's row (#1435) reads the chief's model and worker
        cap from GET /api/config, so showing them needs no passkey. Writes
        stay on PUT /api/board/chief/settings: POST /api/config ignores them."""
        client, app, _ = webapp_client
        body = client.get("/api/config").json()
        assert body["chief_model"] == app.state.webapp_config.chief_model
        assert body["chief_worker_cap"] == app.state.webapp_config.chief_worker_cap
        assert (body["chief_worker_cap_min"], body["chief_worker_cap_max"]) == (1, 10)

        # What a PUT /api/board/chief/settings stores (that route is
        # passkey-gated, covered in tests/api/chief/).
        app.state.webapp_config = update_webapp_config(chief_model="gpt-5.6-terra", chief_worker_cap=5)
        body = client.get("/api/config").json()
        assert (body["chief_model"], body["chief_worker_cap"]) == ("gpt-5.6-terra", 5)

        client.post("/api/config", json={"chief_model": "gpt-5.6-sol", "chief_worker_cap": 2})
        assert (app.state.webapp_config.chief_model, app.state.webapp_config.chief_worker_cap) == ("gpt-5.6-terra", 5)

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

    def test_fleet_config_dir_round_trips(self, webapp_client):
        """fleet_config_dir is in the allow-list —
        it patches through and surfaces on the next GET."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"fleet_config_dir": "E:\\automation\\fleet-config"}
        )
        assert resp.status_code == 200
        assert (
            app.state.webapp_config.fleet_config_dir
            == "E:\\automation\\fleet-config"
        )
        body = client.get("/api/config").json()
        assert body["fleet_config_dir"] == "E:\\automation\\fleet-config"


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


    def test_copilot_launch_options_round_trip_into_the_computed_flags(self, webapp_client):
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        resp = client.post("/api/config", json={
            "copilot_model": "gpt-5.6-terra", "copilot_autopilot": False,
            "copilot_context": "default", "copilot_effort": "high",
        })
        assert resp.status_code == 200, resp.text
        flags = resp.json()["copilot"]["computed_flags"]
        assert flags == "--model gpt-5.6-terra --context default --effort high"
        on_disk = load_webapp_config(overrides["tmp_webapp_cfg_path"])
        assert (on_disk.copilot_model, on_disk.copilot_autopilot) == ("gpt-5.6-terra", False)

    @pytest.mark.parametrize("model", ["", "default"])
    def test_the_auto_model_sends_no_model_and_no_effort(self, webapp_client, model):
        """Copilot's auto model rejects --effort, so it only rides with an explicit model."""
        client, _, _ = webapp_client
        resp = client.post("/api/config", json={"copilot_model": model, "copilot_effort": "high"})
        assert resp.status_code == 200, resp.text
        flags = resp.json()["copilot"]["computed_flags"]
        assert "--model" not in flags and "--effort" not in flags
        assert "--autopilot" in flags

    def test_copilot_models_list_is_trimmed_deduplicated_and_never_empty(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"copilot_models": [" gpt-a ", "gpt-b", "gpt-a", ""]})
        assert resp.status_code == 200, resp.text
        assert app.state.webapp_config.copilot_models == ["gpt-a", "gpt-b"]
        assert client.get("/api/config").json()["copilot"]["models_available"] == ["gpt-a", "gpt-b"]
        for bad in ([], ["", "  "], "gpt-a", [1], None):
            assert client.post("/api/config", json={"copilot_models": bad}).status_code == 400, bad
        assert app.state.webapp_config.copilot_models == ["gpt-a", "gpt-b"]

    def test_settings_offers_default_models_a_saved_list_lacks(self, webapp_client):
        """A saved list replaces the default, so a newer default id is offered, not lost."""
        client, app, _ = webapp_client
        saved = ["gpt-5.6-luna", "custom-x"]
        assert client.post("/api/config", json={"copilot_models": saved}).status_code == 200
        copilot = client.get("/api/config").json()["copilot"]
        assert "gpt-6-luna" in copilot["models_missing_defaults"]
        assert "gpt-5.6-luna" not in copilot["models_missing_defaults"]
        # What the Settings Add button sends: the saved list plus the missing ids.
        added = copilot["models_available"] + copilot["models_missing_defaults"]
        assert client.post("/api/config", json={"copilot_models": added}).status_code == 200
        after = client.get("/api/config").json()["copilot"]
        assert after["models_available"][:2] == saved
        assert "gpt-6-luna" in after["models_available"]
        assert after["models_missing_defaults"] == []

    @pytest.mark.parametrize(
        "key", ["copilot_skip_permissions", "copilot_autopilot", "copilot_remote_session"]
    )
    @pytest.mark.parametrize("junk", ["true", "false", 0, 1, None])
    def test_copilot_switches_reject_non_booleans(self, webapp_client, key, junk):
        client, app, _ = webapp_client
        before = getattr(app.state.webapp_config, key)
        assert client.post("/api/config", json={key: junk}).status_code == 400
        assert getattr(app.state.webapp_config, key) is before

    def test_a_retired_agent_setting_is_ignored_not_saved(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"claude_effort": "low", "codex_model": "x"})
        assert resp.status_code == 200
        assert not hasattr(app.state.webapp_config, "codex_model")

    def test_the_gitlab_forge_settings_round_trip_trimmed(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"gitlab_group": "  my-group ", "gitlab_host": " git.example.test "}
        )
        assert resp.status_code == 200, resp.text
        cfg = app.state.webapp_config
        assert (cfg.gitlab_group, cfg.gitlab_host) == ("my-group", "git.example.test")
        assert client.get("/api/config").json()["gitlab_group"] == "my-group"

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


class TestPreventLockSwitch:
    def test_off_by_default_and_saved_through_the_config_post(self, webapp_client):
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["prevent_lock"] is False
        assert client.post("/api/config", json={"prevent_lock": True}).status_code == 200
        assert app.state.webapp_config.prevent_lock is True
        assert client.get("/api/config").json()["prevent_lock"] is True
        # The setting survives a restart: a fresh load from disk reads it back.
        assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).prevent_lock is True
        client.post("/api/config", json={"prevent_lock": False})
        assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).prevent_lock is False

    def test_a_truthy_string_is_refused(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"prevent_lock": "false"})
        assert resp.status_code == 400
        assert app.state.webapp_config.prevent_lock is False


class TestLocalHubUrl:
    """Settings > Local hub: the address is checked against the hub before it is saved."""

    @staticmethod
    def _hub(monkeypatch, *, down: str = ""):
        from src.clients import hub_health

        probed = []

        def check(url):
            probed.append(url)
            if down:
                raise hub_health.HubHealthError(down, status=400)

        monkeypatch.setattr(hub_health, "check_answers", check)
        return probed

    def test_it_is_set_and_cleared_without_a_restart(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        probed = self._hub(monkeypatch)
        assert client.get("/api/system/health").json()["why"] == "disabled"
        assert client.get("/api/config").json()["llm_hub_local_url"] == "http://127.0.0.1:8020"

        assert client.post("/api/config", json={"llm_hub_url": " http://127.0.0.1:8020/ "}).status_code == 200
        assert probed == ["http://127.0.0.1:8020"]
        assert app.state.webapp_config.llm_hub_url == "http://127.0.0.1:8020"
        assert client.get("/api/config").json()["llm_hub_url"] == "http://127.0.0.1:8020"
        assert client.get("/api/system/health").json()["why"] != "disabled"

        assert client.post("/api/config", json={"llm_hub_url": ""}).status_code == 200
        assert probed == ["http://127.0.0.1:8020"]  # clearing asks nothing
        assert app.state.webapp_config.llm_hub_url == ""
        assert client.get("/api/system/health").json()["why"] == "disabled"

    def test_an_unreachable_hub_is_refused_with_its_reason_and_nothing_is_saved(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        self._hub(monkeypatch, down="Nothing is listening at http://127.0.0.1:8020")
        resp = client.post("/api/config", json={"llm_hub_url": "http://127.0.0.1:8020"})
        assert resp.status_code == 400
        assert "Nothing is listening" in resp.json()["detail"]
        assert app.state.webapp_config.llm_hub_url == ""

    def test_a_bad_address_is_refused_before_the_hub_is_asked(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        probed = self._hub(monkeypatch)
        resp = client.post("/api/config", json={"llm_hub_url": "http://example.com"})
        assert resp.status_code == 400
        assert "private network" in resp.json()["detail"]
        assert probed == [] and app.state.webapp_config.llm_hub_url == ""
