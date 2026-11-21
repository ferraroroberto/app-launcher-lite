"""The chief's settings: auto-compact threshold, model and worker cap."""

from __future__ import annotations

import pytest


class TestChiefAutoCompactThreshold:
    """#1298: the chief's auto-compact threshold (fleet-config#1052), set in
    Settings through the ordinary /api/config save and read by
    ``chief_ops.py self-compact`` from GET /api/board/chief-settings. 0 is
    off; otherwise 10-90 percent; 30 when nothing is stored."""

    def test_endpoint_returns_the_default_when_nothing_is_stored(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/board/chief-settings")
        assert resp.status_code == 200
        assert resp.json() == {"auto_compact_threshold": 30}

    @pytest.mark.parametrize("value", [45, 10, 90, 0])
    def test_settings_save_persists_and_the_endpoint_reads_it(self, webapp_client, value):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_auto_compact_threshold": value})
        assert resp.status_code == 200, resp.text
        assert app.state.webapp_config.chief_auto_compact_threshold == value
        assert client.get("/api/config").json()["chief_auto_compact_threshold"] == value
        assert client.get("/api/board/chief-settings").json() == {"auto_compact_threshold": value}
        # Persisted to the config file, not just process state.
        from src.webapp_config import load_webapp_config
        assert load_webapp_config().chief_auto_compact_threshold == value

    @pytest.mark.parametrize("bad", [5, 9, 91, 100, -1])
    def test_out_of_range_is_rejected_with_a_clear_message(self, webapp_client, bad):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_auto_compact_threshold": bad})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "chief_auto_compact_threshold" in detail and "0 (off)" in detail, detail
        assert "10" in detail and "90" in detail, detail
        assert app.state.webapp_config.chief_auto_compact_threshold == 30

    @pytest.mark.parametrize("bad", ["lots", None, 30.5, True])
    def test_a_non_integer_is_rejected(self, webapp_client, bad):
        client, _, _ = webapp_client
        resp = client.post("/api/config", json={"chief_auto_compact_threshold": bad})
        assert resp.status_code == 400
        assert "integer" in resp.json()["detail"]

    def test_endpoint_sits_behind_the_board_token(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        assert client.get("/api/board/chief-settings").status_code == 401
        ok = client.get(
            "/api/board/chief-settings", headers={"Authorization": "Bearer secret-token"}
        )
        assert ok.status_code == 200 and ok.json() == {"auto_compact_threshold": 30}


class TestChiefSettings:

    def test_get_returns_defaults(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        resp = client.get("/api/board/chief/settings")
        assert resp.status_code == 200
        assert resp.json()["settings"] == {
            "model": "gpt-6-luna",
            "model_problem": "",
            "context": "",
            "effort": "",
            "worker_cap": 3,
            "worker_cap_scope": "chief",
            "auto_drain": True,
            "chief_job_self_heal": False,
            "chief_weekly_proposals": False,
        }

    @pytest.mark.parametrize("key", ["chief_job_self_heal", "chief_weekly_proposals"])
    def test_automation_switches_follow_the_config_and_survive_a_put(
        self, webapp_client, _bypass_gate, key,
    ):
        """fleet-config reads each Automations switch from this block, so a
        Settings flip shows here at once and a chief PUT does not reset it."""
        client, _, _ = webapp_client
        assert client.post("/api/config", json={key: True}).status_code == 200
        assert client.get("/api/board/chief/settings").json()["settings"][key] is True
        resp = client.put("/api/board/chief/settings", json={"worker_cap": 4})
        assert resp.json()["settings"][key] is True

    def test_put_persists_and_reloads(self, webapp_client, _bypass_gate):
        client, app, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings",
            json={"model": "gpt-5.6-terra", "worker_cap": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["settings"]["model"] == "gpt-5.6-terra"
        assert resp.json()["settings"]["worker_cap"] == 5
        assert app.state.webapp_config.chief_model == "gpt-5.6-terra"
        # Round-trips through the persisted file, not just process state.
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["worker_cap"] == 5

    def test_worker_cap_scope_persists_and_survives_other_puts(self, webapp_client, _bypass_gate):
        client, app, _ = webapp_client
        resp = client.put("/api/board/chief/settings", json={"worker_cap_scope": "all"})
        assert resp.status_code == 200
        assert resp.json()["settings"]["worker_cap_scope"] == "all"
        assert app.state.webapp_config.chief_worker_cap_scope == "all"
        # A later PUT of another field leaves the scope alone, and the stored
        # value is what the loopback reader gets.
        client.put("/api/board/chief/settings", json={"worker_cap": 6})
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["worker_cap_scope"] == "all"
        assert client.get("/api/config").json()["chief_worker_cap_scope"] == "all"
        resp = client.put("/api/board/chief/settings", json={"worker_cap_scope": "chief"})
        assert resp.json()["settings"]["worker_cap_scope"] == "chief"


    def test_auto_drain_follows_the_config_switch(self, webapp_client, _bypass_gate):
        """A fresh config reports auto_drain true. Turning the switch off
        persists, and a later cap save does not turn it back on."""
        client, _, _ = webapp_client
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["auto_drain"] is True
        assert client.post(
            "/api/config", json={"chief_auto_drain": False}
        ).status_code == 200
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["auto_drain"] is False
        resp = client.put("/api/board/chief/settings", json={"worker_cap": 4})
        assert resp.json()["settings"]["auto_drain"] is False

    def test_context_and_effort_persist_and_read_back(self, webapp_client, _bypass_gate):
        client, app, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings", json={"context": "default", "effort": "high"},
        )
        assert resp.status_code == 200
        assert (resp.json()["settings"]["context"], resp.json()["settings"]["effort"]) == (
            "default", "high",
        )
        assert (app.state.webapp_config.chief_context, app.state.webapp_config.chief_effort) == (
            "default", "high",
        )
        # The read-only config block Settings renders from carries them too, and
        # a PUT that names neither leaves them alone.
        client.put("/api/board/chief/settings", json={"worker_cap": 4})
        body = client.get("/api/config").json()
        assert (body["chief_context"], body["chief_effort"]) == ("default", "high")
        # An empty value clears the override (follow the Coding agent).
        client.put("/api/board/chief/settings", json={"context": "", "effort": ""})
        assert app.state.webapp_config.chief_context == ""
        assert app.state.webapp_config.chief_effort == ""

    @pytest.mark.parametrize("bad", [{"context": "huge"}, {"effort": "extreme"}])
    def test_context_and_effort_reject_values_copilot_does_not_accept(
        self, webapp_client, _bypass_gate, bad,
    ):
        client, app, _ = webapp_client
        assert client.put("/api/board/chief/settings", json=bad).status_code == 400
        assert (app.state.webapp_config.chief_context, app.state.webapp_config.chief_effort) == ("", "")

    def test_a_chief_model_outside_the_list_is_reported_not_hidden(
        self, webapp_client, _bypass_gate,
    ):
        client, app, _ = webapp_client
        app.state.webapp_config.chief_model = "no-such-model"
        assert "no-such-model" in client.get("/api/config").json()["chief_model_problem"]
        assert client.get("/api/board/chief/settings").json()["settings"]["model_problem"]

    def test_put_ignores_unknown_keys(self, webapp_client, _bypass_gate):
        """#616: a stale client still sending the retired respawn keys must
        not error or resurrect them — they're simply not recognized patch
        keys, so a request carrying only those (plus nothing else valid)
        falls through to the generic empty-patch 400, same as any other
        unrecognized body."""
        client, _, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings",
            json={"respawn_enabled": False, "respawn_at": "06:30"},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("bad", [
        {"model": "haiku"},
        {"model": "gpt5.6"},
        {"worker_cap": 0},
        {"worker_cap": 99},
        {"worker_cap": "lots"},
        {"worker_cap_scope": "everyone"},
        {"worker_cap_scope": ""},
        {},
    ])
    def test_put_rejects_bad_values(self, webapp_client, _bypass_gate, bad):
        client, _, _ = webapp_client
        assert client.put(
            "/api/board/chief/settings", json=bad
        ).status_code == 400

    def test_put_accepts_raised_ceiling(self, webapp_client, _bypass_gate):
        """#547: the ceiling was raised 8 -> 10 on direct request; 10 must
        now persist (it 400'd against the old ceiling) and 11 must still
        400 (the ceiling moved, it didn't disappear)."""
        client, _, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings", json={"worker_cap": 10}
        )
        assert resp.status_code == 200
        assert resp.json()["settings"]["worker_cap"] == 10
        assert client.put(
            "/api/board/chief/settings", json={"worker_cap": 11}
        ).status_code == 400


class TestChiefCapRaisedEvent:
    """Saving a higher worker cap (or freeing slots by switching the scope to
    the chief's own sessions) drops one cap-raised event in the chief mailbox;
    a lower or unchanged save drops nothing, and a failed write never fails
    the save."""

    @pytest.fixture
    def inbox(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from src import instance_role

        monkeypatch.setattr(
            instance_role, "canonical_instance", lambda *a, **k: (True, "canonical")
        )
        return tmp_path / "chief-inbox"

    @staticmethod
    def _events(inbox):
        import json

        return [json.loads(p.read_text("utf-8")) for p in sorted(inbox.glob("*.json"))]

    def test_a_higher_cap_writes_one_event_and_rings_the_doorbell(self, webapp_client, inbox):
        import time

        from app.webapp.chief_doorbell import count_pending

        client, _, _ = webapp_client
        assert client.put("/api/board/chief/settings", json={"worker_cap": 5}).status_code == 200
        (event,) = self._events(inbox)
        assert (event["kind"], event["event"], event["old"], event["new"]) == (
            "cap-raised", "CapRaised", 3, 5,
        )
        assert count_pending(inbox, int(time.time() * 1000)) == 1

    @pytest.mark.parametrize("cap", [3, 2])
    def test_an_unchanged_or_lower_cap_writes_nothing(self, webapp_client, inbox, cap):
        client, app, _ = webapp_client
        assert client.put("/api/board/chief/settings", json={"worker_cap": cap}).status_code == 200
        assert app.state.webapp_config.chief_worker_cap == cap
        assert self._events(inbox) == []

    def test_a_scope_switch_to_chief_writes_one_event(self, webapp_client, inbox):
        client, _, _ = webapp_client
        client.put("/api/board/chief/settings", json={"worker_cap_scope": "all"})
        assert self._events(inbox) == []
        client.put("/api/board/chief/settings", json={"worker_cap_scope": "chief"})
        (event,) = self._events(inbox)
        assert (event["old"], event["new"]) == (3, 3)

    def test_a_failed_mailbox_write_does_not_fail_the_save(
        self, webapp_client, inbox, monkeypatch
    ):
        from src.chief import cap_raised

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(cap_raised, "publish_event", boom)
        client, app, _ = webapp_client
        resp = client.put("/api/board/chief/settings", json={"worker_cap": 6})
        assert resp.status_code == 200
        assert resp.json()["settings"]["worker_cap"] == 6
        assert app.state.webapp_config.chief_worker_cap == 6


class TestSkillsDir:
    """The issue-skills folder: a plain Settings field, and the preflight both readers share."""

    def test_saved_through_settings_and_reported_by_both_reads(self, webapp_client, _bypass_gate, tmp_path):
        client, app, _ = webapp_client
        own = tmp_path / "own"
        (own / "issue-start").mkdir(parents=True)
        (own / "issue-start" / "SKILL.md").touch()
        resp = client.post("/api/config", json={"skills_dir": f"  {own}  "})
        assert resp.status_code == 200, resp.text
        assert app.state.webapp_config.skills_dir == str(own)
        cfg = client.get("/api/config").json()
        assert cfg["skills_dir"] == str(own)
        chief = client.get("/api/board/chief/skills").json()
        for body in (cfg["skills_preflight"], chief):
            assert body["source"] == "skills_dir" and body["ready"] is False
            states = {s["name"]: s["state"] for s in body["skills"]}
            assert states == {"issue-add": "missing", "issue-start": "found",
                              "issue-yolo": "missing", "issue-finish": "missing"}

    def test_empty_uses_the_bundled_skills_which_are_all_found(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        body = client.get("/api/board/chief/skills").json()
        assert body["source"] == "bundled" and body["ready"] is True
