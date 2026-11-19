"""GET /api/config shape, the boot-autostart and lid keep-awake switches, the
Copilot block and the Chat send folders."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.boot_autostart as boot_autostart_mod


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
        assert "gitlab_group" in body and "gitlab_host" in body
        assert "fleet_config_dir" in body
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
        # The retired agents' blocks and the full app's panels are gone.
        for gone in ("claude", "codex", "pi", "grok", "antigravity", "model_catalog",
                     "board_dispatch_models", "remote_power_guard", "usage_providers",
                     "life_os_dir", "github_owner"):
            assert gone not in body, gone
        # auth_password_set is what the SPA shows in the sign-in sheet
        # ("a password is required" vs not). Bool, not the password itself.
        assert isinstance(body["auth_password_set"], bool)
        assert body["auth_password_set"] is False  # default conftest config
        assert body["boot_autostart_enabled"] is False  # nothing enabled yet


class TestCopilotBlock:
    def test_block_carries_the_settings_the_choices_and_the_flags(self, webapp_client):
        client, _, _ = webapp_client
        cp = client.get("/api/config").json()["copilot"]
        assert set(cp) == {
            "skip_permissions", "models_available", "model", "autopilot", "context",
            "contexts_available", "effort", "efforts_available", "computed_flags",
        }
        assert isinstance(cp["skip_permissions"], bool)
        assert cp["model"] in cp["models_available"]
        assert cp["context"] in cp["contexts_available"]
        assert cp["effort"] in cp["efforts_available"]

    def test_computed_flags_follow_the_defaults(self, webapp_client):
        client, _, _ = webapp_client
        cp = client.get("/api/config").json()["copilot"]
        # An explicit default model carries its effort; autopilot is on, allow-all is not.
        assert cp["computed_flags"] == (
            f"--model {cp['model']} --autopilot --context {cp['context']} --effort {cp['effort']}"
        )

    def test_status_no_longer_reports_voice_ocr_tts(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/status").json()
        for gone in ("voice_dictation", "screenshot_ocr", "tts"):
            assert gone not in body, gone


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


class TestLidKeepAwake:
    """POST /api/settings/lid-keep-awake and its /api/config block, with
    powercfg faked: no test changes a real power plan."""

    PLAN = "381b4222-f694-41f0-9685-ff5bb260df2e"

    @pytest.fixture
    def powercfg(self, monkeypatch) -> dict:
        import subprocess

        import src.desktop.lid_keep_awake as lid_mod

        fake = {"values": [1, 2], "lid": True, "fail": False}

        def run(args):
            args = list(args)
            if fake["fail"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="denied")
            if args[0] == "/q":
                if not fake["lid"]:
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                ac, dc = fake["values"]
                out = (f"Power Scheme GUID: {self.PLAN}  (Balanced)\n"
                       f"  Current AC Power Setting Index: 0x{ac:08x}\n"
                       f"  Current DC Power Setting Index: 0x{dc:08x}\n")
                return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
            if args[0] in ("/setacvalueindex", "/setdcvalueindex"):
                fake["values"][0 if args[0] == "/setacvalueindex" else 1] = int(args[-1])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(lid_mod, "_run_powercfg", run)
        return fake

    def test_config_reports_the_live_state(self, webapp_client, powercfg):
        client, _, _ = webapp_client
        assert client.get("/api/config").json()["lid_keep_awake"] == {"state": "off", "reason": None}
        powercfg["values"] = [0, 0]
        assert client.get("/api/config").json()["lid_keep_awake"]["state"] == "on"
        powercfg["lid"] = False
        assert client.get("/api/config").json()["lid_keep_awake"]["state"] == "unsupported"
        powercfg["fail"] = True
        assert client.get("/api/config").json()["lid_keep_awake"]["state"] == "unknown"

    def test_turning_on_then_off_round_trips_the_values(self, webapp_client, powercfg):
        client, _, _ = webapp_client
        resp = client.post("/api/settings/lid-keep-awake", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["lid_keep_awake"]["state"] == "on"
        assert powercfg["values"] == [0, 0]
        resp = client.post("/api/settings/lid-keep-awake", json={"enabled": False})
        assert resp.json()["lid_keep_awake"]["state"] == "off"
        assert powercfg["values"] == [1, 2]

    def test_a_failure_is_a_400_with_the_plain_reason(self, webapp_client, powercfg):
        client, _, _ = webapp_client
        powercfg["fail"] = True
        resp = client.post("/api/settings/lid-keep-awake", json={"enabled": True})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Windows power settings refused the request."

    def test_a_pc_with_no_lid_is_a_400_not_a_silent_ok(self, webapp_client, powercfg):
        client, _, _ = webapp_client
        powercfg["lid"] = False
        resp = client.post("/api/settings/lid-keep-awake", json={"enabled": True})
        assert resp.status_code == 400
        assert "no lid" in resp.json()["detail"]


class TestOutboxScratchRoots:
    """Settings' Chat file folders (#1748): POST /api/config checks each
    folder on write; the loader stays lenient about one that has gone."""

    def _post(self, client, roots):
        return client.post("/api/config", json={"outbox_scratch_roots": roots})

    @pytest.mark.windows_only
    def test_round_trips_as_typed_and_reaches_disk(self, webapp_client, tmp_path, monkeypatch):
        from src.webapp_config import load_webapp_config

        client, app, _ = webapp_client
        drop = tmp_path / "Drop"
        drop.mkdir()
        monkeypatch.setenv("OUTBOX_TEST_BASE", str(tmp_path))
        typed = ["  %OUTBOX_TEST_BASE%/Drop  ", str(drop / ".." / "Drop")]
        # The two spellings are the same folder: a duplicate, refused.
        assert self._post(client, typed).status_code == 400
        resp = self._post(client, [typed[0], "  "])
        assert resp.status_code == 200
        # The typed text is stored (trimmed, %VAR% kept), never the resolved path.
        assert app.state.webapp_config.outbox_scratch_roots == ["%OUTBOX_TEST_BASE%/Drop"]
        assert client.get("/api/config").json()["outbox_scratch_roots"] == ["%OUTBOX_TEST_BASE%/Drop"]
        assert load_webapp_config(apply_env_override=False).outbox_scratch_roots == [
            "%OUTBOX_TEST_BASE%/Drop"
        ]

    def test_default_is_in_the_get_payload(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/config").json()["outbox_scratch_roots"] == ["%TEMP%/claude"]

    @pytest.mark.parametrize(
        "bad, message",
        [
            (r"\\host\share", "network and device"),
            ("//host/share", "network and device"),
            (r"\\?\C:\x", "network and device"),
            ("{drive_root}", "whole drive"),
            ("{missing}", "does not exist"),
            ("{a_file}", "file, not a folder"),
        ],
    )
    def test_a_bad_entry_is_refused_in_its_own_words_and_saves_nothing(
        self, webapp_client, tmp_path, bad, message
    ):
        client, app, _ = webapp_client
        good = tmp_path / "good"
        good.mkdir()
        a_file = tmp_path / "plain.txt"
        a_file.write_text("x", encoding="utf-8")
        assert self._post(client, [str(good)]).status_code == 200
        bad = bad.format(
            drive_root=Path(tmp_path.anchor), missing=tmp_path / "gone", a_file=a_file
        )
        resp = self._post(client, [str(good), bad])
        assert resp.status_code == 400
        assert message in resp.json()["detail"]
        assert app.state.webapp_config.outbox_scratch_roots == [str(good)]

    @pytest.mark.windows_only
    def test_a_duplicate_is_refused_by_case_and_separator(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        folder = tmp_path / "Dup"
        folder.mkdir()
        resp = self._post(client, [str(folder), str(folder).upper().replace("\\", "/")])
        assert resp.status_code == 400
        assert "already in the list" in resp.json()["detail"]

    def test_at_least_one_folder_must_stay(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        for empty in ([], ["", "   "]):
            resp = self._post(client, empty)
            assert resp.status_code == 400
            assert "at least one folder" in resp.json()["detail"]
        assert app.state.webapp_config.outbox_scratch_roots == ["%TEMP%/claude"]

    @pytest.mark.parametrize("body", ["a-string", {"a": 1}, None, 5, [1, 2], [["x"]]])
    def test_a_non_list_of_strings_never_reaches_the_config(self, webapp_client, body):
        client, app, _ = webapp_client
        assert self._post(client, body).status_code == 400
        assert app.state.webapp_config.outbox_scratch_roots == ["%TEMP%/claude"]

    def test_a_since_deleted_folder_still_loads_and_other_settings_still_save(
        self, webapp_client, tmp_path
    ):
        import shutil

        client, app, _ = webapp_client
        folder = tmp_path / "soon-gone"
        folder.mkdir()
        assert self._post(client, [str(folder)]).status_code == 200
        shutil.rmtree(folder)
        # The write-time rules are the router's: load, and an unrelated save,
        # must not trip over the missing folder.
        resp = client.post("/api/config", json={"copilot_autopilot": False})
        assert resp.status_code == 200
        assert app.state.webapp_config.outbox_scratch_roots == [str(folder)]

    def test_a_stored_folder_that_has_gone_does_not_block_editing_the_others(
        self, webapp_client, tmp_path
    ):
        import shutil

        client, app, _ = webapp_client
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert self._post(client, [str(a), str(b)]).status_code == 200
        shutil.rmtree(a)
        # Removing b keeps the gone a; only a NEW entry is checked.
        assert self._post(client, [str(a)]).status_code == 200
        fresh = tmp_path / "c"
        fresh.mkdir()
        assert self._post(client, [str(a), str(fresh)]).status_code == 200
        assert self._post(client, [str(a), str(tmp_path / "never")]).status_code == 400

    def test_adding_a_stored_folder_again_is_a_duplicate(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        folder = tmp_path / "once"
        folder.mkdir()
        assert self._post(client, [str(folder)]).status_code == 200
        resp = self._post(client, [str(folder), str(folder)])
        assert resp.status_code == 400
        assert "already in the list" in resp.json()["detail"]
        assert app.state.webapp_config.outbox_scratch_roots == [str(folder)]
