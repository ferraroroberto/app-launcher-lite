"""Jobs API — CRUD, job-kind registry, run, typed params and cooldown."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from tests.unit.jobs._jobs_api_helpers import _seed_one_job, _stub_path

# =================================================================== CRUD


class TestCreateJob:
    def test_minimal_create(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = _seed_one_job(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["job"]["name"] == "Demo"
        # Default schedule chip for type=none is empty string.
        assert body["job"]["schedule_chip"] == ""
        # Schtasks sync was called for the new job.
        assert mocked_jobs_side_effects["sync_schtasks"].called

    def test_missing_name_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={"name": "", "script_path": "C:\\stub\\demo.bat"},
        )
        assert resp.status_code == 400

    def test_missing_script_path_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs", json={"name": "Demo"})
        assert resp.status_code == 400

    def test_bad_suffix_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={"name": "X", "script_path": "C:\\stub\\x.txt"},
        )
        assert resp.status_code == 400

    def test_daily_times_round_trips(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = _seed_one_job(
            client,
            schedule={"type": "daily_times", "at": ["06:00", "12:00", "18:00"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["job"]["schedule"]["type"] == "daily_times"
        assert body["job"]["schedule"]["at"] == ["06:00", "12:00", "18:00"]

    def test_duplicate_id_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        first = _seed_one_job(client, name="Demo")
        assert first.status_code == 200
        # Same name → same slug id → 409.
        second = client.post(
            "/api/jobs",
            json={
                "id": first.json()["job"]["id"],
                "name": "Demo",
                "script_path": _stub_path("demo.bat"),
            },
        )
        assert second.status_code == 409


class TestListJobs:
    def test_empty_initially(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert resp.json() == {"jobs": []}

    def test_lists_after_create(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        _seed_one_job(client, name="Alpha")
        _seed_one_job(client, name="Bravo")
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        names = [j["name"] for j in resp.json()["jobs"]]
        assert names == ["Alpha", "Bravo"]


class TestEditJob:
    def test_404_on_unknown(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.put("/api/jobs/nope", json={"name": "X"})
        assert resp.status_code == 404

    def test_changes_name_and_resyncs(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        mocked_jobs_side_effects["sync_schtasks"].reset_mock()
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"name": "Renamed", "schedule": {"type": "daily", "at": "07:00"}},
        )
        assert resp.status_code == 200
        assert resp.json()["job"]["name"] == "Renamed"
        # Edits must re-sync schtasks so the schedule change lands.
        assert mocked_jobs_side_effects["sync_schtasks"].called


class TestDeleteJob:
    def test_404_on_unknown(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.delete("/api/jobs/nope")
        assert resp.status_code == 404

    def test_removes_and_deletes_schtasks(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.delete("/api/jobs/" + created["id"])
        assert resp.status_code == 200
        assert resp.json()["removed"] == created["id"]
        # And the schtasks deletion was attempted.
        assert mocked_jobs_side_effects["delete_schtasks"].called


# ========================================================== job-kind (#70)


class TestJobKindRegistry:
    def test_create_inline_shell(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Inline demo",
                "kind": "inline-shell",
                "kind_config": {"script_body": "echo hi", "ext": ".bat"},
                "schedule": {"type": "none"},
            },
        )
        assert resp.status_code == 200, resp.text
        job = resp.json()["job"]
        assert job["kind"] == "inline-shell"
        assert job["kind_config"] == {"script_body": "echo hi", "ext": ".bat"}
        assert job["script_path"] == ""
        assert job["target_kind"] == "inline-shell"

    def test_create_http_check(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Health check",
                "kind": "http-check",
                "kind_config": {"url": "https://example.com/health"},
                "schedule": {"type": "hourly", "every": 1},
            },
        )
        assert resp.status_code == 200, resp.text
        job = resp.json()["job"]
        assert job["kind"] == "http-check"
        assert job["target_kind"] == "http-check"

    def test_create_http_check_missing_url_is_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Health check",
                "kind": "http-check",
                "kind_config": {},
                "schedule": {"type": "none"},
            },
        )
        assert resp.status_code == 400

    def test_create_unknown_kind_is_400(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Bogus",
                "kind": "bogus-kind",
                "script_path": _stub_path("demo.bat"),
                "schedule": {"type": "none"},
            },
        )
        assert resp.status_code == 400

    def test_switch_file_kind_to_inline_shell(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={
                "kind": "inline-shell",
                "script_path": "",
                "kind_config": {"script_body": "echo hi", "ext": ".bat"},
            },
        )
        assert resp.status_code == 200, resp.text
        job = resp.json()["job"]
        assert job["kind"] == "inline-shell"
        assert job["script_path"] == ""

    def test_switch_to_inline_shell_without_clearing_script_path_is_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        # Switching kind without explicitly clearing script_path in the
        # same PUT body is rejected — the dialog always sends the full
        # payload (including an explicit clear), so this only happens on
        # a malformed/partial PUT.
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={
                "kind": "inline-shell",
                "kind_config": {"script_body": "echo hi", "ext": ".bat"},
            },
        )
        assert resp.status_code == 400


# =================================================================== /run


class TestRunJob:
    def test_404_on_unknown(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs/nope/run")
        assert resp.status_code == 404

    def test_spawns_and_returns_run_id(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post("/api/jobs/" + created["id"] + "/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == created["id"]
        run_id = body["run_id"]
        assert run_id
        # The executor was spawned detached with the same run id.
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        assert spawn.called
        assert spawn.call_args.args[1] == run_id


# ================================================================== params


class TestParamsCRUD:
    """Params (issue #67) round-trip through create + edit."""

    def test_create_accepts_params(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Scrape",
                "script_path": _stub_path("scrape.py"),
                "params": [
                    {"name": "since", "kind": "date", "flag": "--since"},
                    {"name": "verbose", "kind": "bool", "flag": "--verbose",
                     "default": False},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        params = resp.json()["job"]["params"]
        assert [p["name"] for p in params] == ["since", "verbose"]

    def test_create_rejects_bad_param_shape(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "X", "script_path": "C:\\stub\\x.py",
                "params": [{"name": "x", "kind": "bogus"}],
            },
        )
        assert resp.status_code == 400

    def test_edit_replaces_params(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"params": [{"name": "n", "kind": "int", "flag": "--n"}]},
        )
        assert resp.status_code == 200
        params = resp.json()["job"]["params"]
        assert params and params[0]["name"] == "n"


class TestRunJobWithParams:
    def _seed_param_job(self, client):
        return client.post(
            "/api/jobs",
            json={
                "name": "Scrape",
                "script_path": _stub_path("scrape.py"),
                "params": [
                    {"name": "since", "kind": "date", "flag": "--since"},
                    {"name": "tier", "kind": "enum",
                     "options": ["a", "b"], "default": "a", "flag": "--tier"},
                ],
            },
        ).json()["job"]

    def test_run_with_valid_params(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"since": "2026-06-01", "tier": "b"}},
        )
        assert resp.status_code == 200, resp.text
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        # spawn was passed the validated params payload (4th positional arg).
        assert spawn.call_args.args[3] == {"since": "2026-06-01", "tier": "b"}

    def test_missing_required_returns_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"tier": "a"}},
        )
        assert resp.status_code == 400
        assert "since" in resp.json()["detail"]

    def test_unknown_param_returns_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"since": "2026-06-01", "bogus": 1}},
        )
        assert resp.status_code == 400
        assert "bogus" in resp.json()["detail"]

    def test_enum_not_in_options_returns_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"since": "2026-06-01", "tier": "c"}},
        )
        assert resp.status_code == 400

    def test_empty_body_still_works_for_parameterless_job(
        self, webapp_client, mocked_jobs_side_effects
    ):
        # Regression: parameter-less jobs must keep their one-tap fire.
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post("/api/jobs/" + created["id"] + "/run")
        assert resp.status_code == 200
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        # 4th positional arg is None (no params payload).
        assert spawn.call_args.args[3] is None


# ================================================================ cooldown


class TestCooldown:
    """``cooldown_seconds`` admission gate on POST /api/jobs/<id>/run.

    Cooldown is measured from the most recent non-skipped run's
    ``started_at``. We seed a real run.json under the job's runs dir
    (the fixture redirects ``JOBS_RUNS_DIR`` to ``tmp_path``) and then
    fire ``/run`` — no executor actually spawns (it's mocked).
    """

    def _seed_run_record(
        self, runs_root, job_id, run_id, *, started_at, status="success"
    ):
        run_dir = runs_root / job_id / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "job_id": job_id,
                    "status": status,
                    "started_at": started_at,
                }
            ),
            encoding="utf-8",
        )

    def test_no_cooldown_allows_back_to_back(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        assert client.post(f"/api/jobs/{created['id']}/run").status_code == 200
        assert client.post(f"/api/jobs/{created['id']}/run").status_code == 200

    def test_inside_window_returns_429(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, overrides = webapp_client
        created = client.post(
            "/api/jobs",
            json={
                "name": "Cool",
                "script_path": _stub_path("x.py"),
                "cooldown_seconds": 30,
            },
        ).json()["job"]
        # Anchor — 5 seconds ago, well inside the 30 s window.
        now = datetime.now().replace(microsecond=0)
        anchor_started = (now - timedelta(seconds=5)).isoformat(timespec="seconds")
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"],
            created["id"],
            "20260101T000000",
            started_at=anchor_started,
        )
        resp = client.post(f"/api/jobs/{created['id']}/run")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        retry = int(resp.headers["Retry-After"])
        assert 1 <= retry <= 30
        body = resp.json()
        # FastAPI wraps the structured detail under "detail".
        detail = body["detail"]
        assert detail["detail"] == "cooldown"
        assert detail["cooldown_seconds"] == 30
        assert 1 <= detail["retry_after_seconds"] <= 30
        # Executor must not have been spawned for the rejected fire.
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        assert not spawn.called

    def test_anchor_ignores_skipped_records(
        self, webapp_client, mocked_jobs_side_effects
    ):
        """A `skipped` record sitting on top of an older real run must NOT
        become the cooldown anchor — otherwise rapid mash-fires would keep
        extending the window, turning cooldown into sliding debounce."""
        client, _, overrides = webapp_client
        created = client.post(
            "/api/jobs",
            json={
                "name": "Cool",
                "script_path": _stub_path("x.py"),
                "cooldown_seconds": 30,
            },
        ).json()["job"]
        now = datetime.now().replace(microsecond=0)
        # Real run started 60 s ago — outside the 30 s window.
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"],
            created["id"],
            "20260101T000000",
            started_at=(now - timedelta(seconds=60)).isoformat(timespec="seconds"),
            status="success",
        )
        # A skipped record from 5 s ago — must be ignored by the anchor.
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"],
            created["id"],
            "20260101T000060",
            started_at=(now - timedelta(seconds=5)).isoformat(timespec="seconds"),
            status="skipped",
        )
        resp = client.post(f"/api/jobs/{created['id']}/run")
        assert resp.status_code == 200, resp.text

    def test_cooldown_round_trips_through_put(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"cooldown_seconds": 45},
        )
        assert resp.status_code == 200
        assert resp.json()["job"]["cooldown_seconds"] == 45
        # PUT cooldown_seconds=0 clears it.
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"cooldown_seconds": 0},
        )
        assert resp.status_code == 200
        assert "cooldown_seconds" not in resp.json()["job"]
