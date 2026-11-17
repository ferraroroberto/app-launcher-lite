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
