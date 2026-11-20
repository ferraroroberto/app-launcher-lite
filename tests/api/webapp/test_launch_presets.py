"""Launch presets: the config list."""

from __future__ import annotations

import pytest

PRESET = {"name": "Opus deep", "agent": "claude", "model": "opus", "effort": "xhigh"}


class TestPresetConfig:
    def test_none_by_default(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/config").json()["launch_presets"] == []

    def test_save_assigns_an_id_and_round_trips(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [PRESET]})
        assert resp.status_code == 200, resp.text
        saved = client.get("/api/config").json()["launch_presets"]
        assert len(saved) == 1
        assert saved[0]["id"] and saved[0]["name"] == "Opus deep"
        assert app.state.webapp_config.launch_presets == saved

    def test_rename_and_remove_keep_the_id(self, webapp_client):
        client, _, _ = webapp_client
        client.post("/api/config", json={"launch_presets": [PRESET]})
        saved = client.get("/api/config").json()["launch_presets"]
        renamed = [dict(saved[0], name="  Opus   max ")]
        assert client.post("/api/config", json={"launch_presets": renamed}).status_code == 200
        after = client.get("/api/config").json()["launch_presets"]
        assert after[0]["id"] == saved[0]["id"] and after[0]["name"] == "Opus max"
        client.post("/api/config", json={"launch_presets": []})
        assert client.get("/api/config").json()["launch_presets"] == []

    @pytest.mark.parametrize("bad, fragment", [
        (dict(PRESET, effort="turbo"), "not offered"),
        (dict(PRESET, model="retired-alias"), "unsupported"),
        (dict(PRESET, agent="pi"), "per-launch model"),
        (dict(PRESET, name="  "), "needs a name"),
        (dict(PRESET, name="x" * 41), "at most 40"),
    ])
    def test_refuses_what_the_catalog_does_not_offer(self, webapp_client, bad, fragment):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [bad]})
        assert resp.status_code == 400, resp.text
        assert fragment in resp.json()["detail"]
        assert app.state.webapp_config.launch_presets == []

    def test_refuses_a_duplicate_name_and_a_non_list(self, webapp_client):
        client, _, _ = webapp_client
        twin = dict(PRESET, name="OPUS DEEP")
        assert client.post("/api/config", json={"launch_presets": [PRESET, twin]}).status_code == 400
        assert client.post("/api/config", json={"launch_presets": "opus"}).status_code == 400

    def test_a_saved_preset_survives_a_model_leaving_the_catalog(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        client.post("/api/config", json={"launch_presets": [PRESET]})
        from src import webapp_config as wc
        from src.launching import presets

        reloaded = wc.load_webapp_config(wc.save_webapp_config(app.state.webapp_config))
        assert reloaded.launch_presets == app.state.webapp_config.launch_presets
        assert presets.load_launch_presets({"launch_presets": [{"id": "a", "name": "n"}]}) == []


FLASH = {"name": "MAI Flash high", "agent": "copilot", "model": "mai-code-2-flash", "effort": "high"}


class TestCopilotPresets:
    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
    def test_saves_a_flash_preset_at_each_offered_effort(self, webapp_client, effort):
        client, _, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [dict(FLASH, effort=effort)]})
        assert resp.status_code == 200, resp.text
        saved = client.get("/api/config").json()["launch_presets"]
        assert [(p["agent"], p["model"], p["effort"]) for p in saved] == [
            ("copilot", "mai-code-2-flash", effort)
        ]

    def test_refuses_an_effort_the_probed_model_does_not_offer(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [dict(FLASH, effort="minimal")]})
        assert resp.status_code == 400
        assert "not offered by mai-code-2-flash" in resp.json()["detail"]
        assert "low, medium, high, xhigh, max" in resp.json()["detail"]
        assert app.state.webapp_config.launch_presets == []

    def test_luna_takes_none_but_not_minimal(self, webapp_client):
        client, _, _ = webapp_client
        luna = {"name": "Luna long", "agent": "copilot", "model": "gpt-6-luna",
                "effort": "none", "context": "long_context"}
        assert client.post("/api/config", json={"launch_presets": [luna]}).status_code == 200
        saved = client.get("/api/config").json()["launch_presets"]
        assert saved[0]["context"] == "long_context" and saved[0]["effort"] == "none"
        refused = client.post("/api/config", json={"launch_presets": [dict(luna, effort="minimal")]})
        assert refused.status_code == 400
        assert "not offered by gpt-6-luna" in refused.json()["detail"]

    def test_a_model_without_a_catalog_row_takes_any_cli_effort_and_a_context(self, webapp_client):
        client, _, _ = webapp_client
        other = {"name": "Other long", "agent": "copilot", "model": "some-new-model",
                 "effort": "minimal", "context": "long_context"}
        assert client.post("/api/config", json={"launch_presets": [other]}).status_code == 200
        saved = client.get("/api/config").json()["launch_presets"]
        assert saved[0]["context"] == "long_context" and saved[0]["effort"] == "minimal"
        bogus = dict(other, effort="turbo")
        assert client.post("/api/config", json={"launch_presets": [bogus]}).status_code == 400

    @pytest.mark.parametrize("bad, fragment", [
        (dict(FLASH, context="huge"), "context 'huge' is not offered"),
        (dict(FLASH, model="default"), "needs one model id"),
        (dict(FLASH, model="copilot:mai-code-2-flash"), "needs one model id"),
        (dict(PRESET, context="long_context"), "takes no context tier"),
    ])
    def test_refuses_a_bad_model_or_context(self, webapp_client, bad, fragment):
        client, _, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [bad]})
        assert resp.status_code == 400, resp.text
        assert fragment in resp.json()["detail"]

    def test_a_preset_without_a_context_stores_none(self, webapp_client):
        client, _, _ = webapp_client
        client.post("/api/config", json={"launch_presets": [FLASH]})
        assert "context" not in client.get("/api/config").json()["launch_presets"][0]


class TestPresetEditorContract:
    """What the Settings editor reads and the refusals it shows beside a field."""

    def test_config_lists_each_models_efforts_and_the_context_tiers(self, webapp_client):
        client, app, _ = webapp_client
        copilot = client.get("/api/config").json()["copilot"]
        listed = copilot["models_available"]
        assert set(copilot["preset_efforts"]) == set(listed)
        assert copilot["preset_efforts"]["mai-code-2-flash"] == ["low", "medium", "high", "xhigh", "max"]
        assert "xhigh" in copilot["preset_efforts"]["gpt-6-luna"]
        assert "minimal" not in copilot["preset_efforts"]["gpt-6-luna"]
        assert copilot["preset_contexts"] == ["default", "long_context"]
        assert app.state.webapp_config.copilot_models == listed

    @pytest.mark.parametrize("bad, field", [
        (dict(FLASH, effort="minimal"), "effort"),
        (dict(FLASH, context="huge"), "context"),
        (dict(FLASH, model="default"), "model"),
        (dict(FLASH, name=" "), "name"),
    ])
    def test_a_refusal_names_the_field_and_the_row(self, webapp_client, bad, field):
        client, _, _ = webapp_client
        ok = dict(FLASH, name="Fine", effort="low")
        resp = client.post("/api/config", json={"launch_presets": [ok, bad]})
        assert resp.status_code == 400
        body = resp.json()
        assert (body["field"], body["index"]) == (field, 1)
        assert body["detail"]

    def test_a_repeated_name_is_refused_at_the_later_row(self, webapp_client):
        client, _, _ = webapp_client
        twin = dict(FLASH, name="mai flash HIGH")
        resp = client.post("/api/config", json={"launch_presets": [FLASH, twin]})
        assert resp.status_code == 400
        assert (resp.json()["field"], resp.json()["index"]) == ("name", 1)

    def test_a_refusal_about_the_whole_list_has_no_field(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": "flash"})
        assert resp.status_code == 400
        assert (resp.json()["field"], resp.json()["index"]) == ("", -1)
