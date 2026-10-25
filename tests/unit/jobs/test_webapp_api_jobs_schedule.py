"""Jobs API — pause/resume, elevated jobs, once schedules and mutex groups."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.unit.jobs._jobs_api_helpers import _seed_one_job, _stub_path

# =========================================================== pause / resume


class TestPauseResume:
    """``POST /api/jobs/<id>/pause`` and ``/resume`` (issue #68 PR #4)."""

    def test_pause_then_resume(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(
            client, name="Daily",
            schedule={"type": "daily", "at": "06:00"},
        ).json()["job"]
        resp = client.post(f"/api/jobs/{created['id']}/pause")
        assert resp.status_code == 200
        body = resp.json()["job"]
        assert body["paused"] is True
        assert body["schedule"]["type"] == "none"
        assert body["paused_schedule"]["type"] == "daily"
        assert body["paused_schedule"]["at"] == "06:00"
        # The chip on the API response reflects the parked schedule.
        assert "paused" in body["schedule_chip"]
        # The router resynced schtasks (which deletes the entries for a
        # job whose live schedule is now 'none').
        assert mocked_jobs_side_effects["sync_schtasks"].called

        resp = client.post(f"/api/jobs/{created['id']}/resume")
        assert resp.status_code == 200
        body = resp.json()["job"]
        assert body["paused"] is False
        assert body["schedule"]["type"] == "daily"
        assert body["schedule"]["at"] == "06:00"
        assert "paused_schedule" not in body
        assert body["schedule_chip"] == "daily 06:00"

    def test_pause_manual_only_rejected_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(
            client, name="Manual",
            schedule={"type": "none"},
        ).json()["job"]
        resp = client.post(f"/api/jobs/{created['id']}/pause")
        assert resp.status_code == 400

    def test_pause_unknown_404(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs/nope/pause")
        assert resp.status_code == 404


# ============================================ elevated jobs skip schtasks


class TestElevatedJobsSkipTaskScheduler:
    """Issue #352: create/edit on an ``elevated: true`` job
    must never *create* a Task Scheduler entry via the real schtasks
    runner — its ``/RL HIGHEST`` entry is externally-managed. Issue #409:
    it must still *delete* any stale entry left behind by a prior
    non-elevated schedule, so the runner is no longer never-called — only
    never asked to ``/Create``. The same actions on a plain job still call
    it (including create).

    Unlike the other classes here, ``mocked_jobs_side_effects`` is *not*
    used — it stubs ``sync_schtasks``/``delete_schtasks`` wholesale, which
    would hide a regression in the real skip logic. Instead this patches
    only the I/O boundary (``_run_schtasks`` on the module that owns it,
    per ``src/jobs.py``'s own guidance) so the real ``sync_schtasks`` /
    ``delete_schtasks`` run and their behaviour is what's under test.
    """

    @pytest.fixture
    def real_schtasks_runner(self, monkeypatch):
        import subprocess
        from unittest.mock import MagicMock

        from app.webapp.routers import jobs as jobs_router
        from src import jobs_schtasks

        runner = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
        )
        monkeypatch.setattr(jobs_schtasks, "_run_schtasks", runner)
        # query_next_run is a read-only schtasks query unrelated to
        # sync/delete — mock it out so it doesn't add noise to the
        # runner-call assertions below.
        monkeypatch.setattr(
            jobs_router.jobs_mod, "query_next_run", MagicMock(return_value=None)
        )
        monkeypatch.setattr(
            jobs_router.jobs_mod,
            "run_stats",
            MagicMock(
                return_value={
                    "p50": None,
                    "p95": None,
                    "success_rate_30d": None,
                    "completed_count": 0,
                    "last7": [],
                }
            ),
        )
        monkeypatch.setattr(
            jobs_router.jobs_mod, "is_stuck", MagicMock(return_value=False)
        )
        return runner

    def test_elevated_job_never_creates_schtasks_entry(
        self, webapp_client, real_schtasks_runner
    ):
        client, _, _ = webapp_client

        def assert_no_create():
            argvs = [c.args[0] for c in real_schtasks_runner.call_args_list]
            assert not any(argv[:2] == ["schtasks", "/Create"] for argv in argvs)

        resp = client.post(
            "/api/jobs",
            json={
                "name": "Elevated",
                "script_path": _stub_path("elevated.bat"),
                "schedule": {"type": "hourly", "every": 8},
                "elevated": True,
            },
        )
        assert resp.status_code == 200
        created = resp.json()["job"]
        assert created["elevated"] is True
        assert created["manual_run_allowed"] is False
        assert created["schedule_controls_allowed"] is False
        assert_no_create()

        job_id = created["id"]
        resp = client.put(f"/api/jobs/{job_id}", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert_no_create()

        resp = client.post(f"/api/jobs/{job_id}/pause")
        assert resp.status_code == 409
        assert "externally managed" in resp.json()["detail"]
        assert_no_create()

        resp = client.post(f"/api/jobs/{job_id}/resume")
        assert resp.status_code == 409
        assert "externally managed" in resp.json()["detail"]
        assert_no_create()

        resp = client.post(f"/api/jobs/{job_id}/run")
        assert resp.status_code == 409
        assert "manual runs are unavailable" in resp.json()["detail"]
        assert_no_create()

        # Target resolution remains available because it has no side effects.
        resp = client.post(
            f"/api/jobs/{job_id}/run", json={"dry_run": "check"}
        )
        assert resp.status_code == 200
        assert_no_create()

    def test_non_elevated_job_still_calls_runner(
        self, webapp_client, real_schtasks_runner
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Plain",
                "script_path": _stub_path("plain.bat"),
                "schedule": {"type": "daily", "at": "06:00"},
            },
        )
        assert resp.status_code == 200
        created = resp.json()["job"]
        assert real_schtasks_runner.called
        real_schtasks_runner.reset_mock()

        job_id = created["id"]
        resp = client.put(f"/api/jobs/{job_id}", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert real_schtasks_runner.called
        real_schtasks_runner.reset_mock()

        resp = client.post(f"/api/jobs/{job_id}/pause")
        assert resp.status_code == 200
        assert real_schtasks_runner.called
        real_schtasks_runner.reset_mock()

        resp = client.post(f"/api/jobs/{job_id}/resume")
        assert resp.status_code == 200
        assert real_schtasks_runner.called


class TestOnceSchedule:
    """``once`` schedule end-to-end (admission + decorated response)."""

    def test_once_round_trips(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs", json={
            "name": "Once",
            "script_path": _stub_path("once.py"),
            "schedule": {"type": "once", "at": "2026-06-01T14:30"},
        })
        assert resp.status_code == 200, resp.text
        job = resp.json()["job"]
        assert job["schedule"]["type"] == "once"
        assert job["schedule_chip"] == "once 2026-06-01 14:30"


# ============================================================ mutex groups


class TestMutexGroups:
    """Cross-job admission control (issue #68 PR #2).

    Two jobs in the same mutex_group must not have overlapping running
    runs. The route detects the collision and queues the fresh fire;
    the executor's finalisation drains the queue.
    """

    def _seed_run_record(
        self, runs_root, job_id, run_id, *, started_at, status
    ):
        rd = runs_root / job_id / run_id
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "run.json").write_text(
            json.dumps({
                "run_id": run_id,
                "job_id": job_id,
                "status": status,
                "started_at": started_at,
            }),
            encoding="utf-8",
        )

    def _isolate_queue(self, overrides, monkeypatch):
        """Point the queue file at the fixture's tmp jobs root."""
        # JOBS_QUEUE_PATH is owned by src.jobs_queue (issue #315 split) —
        # enqueue_mutex/pop_mutex_entry/etc. resolve it as their own
        # module global, not through the src.jobs facade.
        from src import jobs_queue as jobs_queue_mod
        monkeypatch.setattr(
            jobs_queue_mod, "JOBS_QUEUE_PATH",
            overrides["tmp_jobs_runs_dir"] / "_queue.json",
        )

    def test_collision_queues_instead_of_spawning(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        client, _, overrides = webapp_client
        self._isolate_queue(overrides, monkeypatch)
        # Two jobs sharing one mutex group.
        a = client.post("/api/jobs", json={
            "name": "A", "script_path": _stub_path("a.py"),
            "mutex_group": "chrome",
        }).json()["job"]
        b = client.post("/api/jobs", json={
            "name": "B", "script_path": _stub_path("b.py"),
            "mutex_group": "chrome",
        }).json()["job"]
        # A is "running" right now.
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"], a["id"], "20260101T000000",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="running",
        )
        resp = client.post(f"/api/jobs/{b['id']}/run")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["mutex_blocked_by"] == a["id"]
        # The executor was NOT spawned (B is queued, not started).
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        assert not spawn.called
        # The on-disk run record is also queued, with the blocker name.
        run_dir = (
            overrides["tmp_jobs_runs_dir"] / b["id"] / body["run_id"]
        )
        record = json.loads(
            (run_dir / "run.json").read_text(encoding="utf-8")
        )
        assert record["status"] == "queued"
        assert record["mutex_group"] == "chrome"
        assert record["mutex_blocked_by"] == a["id"]

    def test_no_collision_when_other_is_done(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        client, _, overrides = webapp_client
        self._isolate_queue(overrides, monkeypatch)
        a = client.post("/api/jobs", json={
            "name": "A", "script_path": _stub_path("a.py"),
            "mutex_group": "db",
        }).json()["job"]
        b = client.post("/api/jobs", json={
            "name": "B", "script_path": _stub_path("b.py"),
            "mutex_group": "db",
        }).json()["job"]
        # A's most recent run is completed → not a collision.
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"], a["id"], "20260101T000000",
            started_at=(datetime.now() - timedelta(minutes=2))
                .isoformat(timespec="seconds"),
            status="success",
        )
        resp = client.post(f"/api/jobs/{b['id']}/run")
        assert resp.status_code == 200
        body = resp.json()
        # No status=queued field on the happy path; executor spawned.
        assert "status" not in body
        assert mocked_jobs_side_effects["spawn_run_job_detached"].called

    def test_mutex_group_round_trips_through_put(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        # Set it.
        r = client.put(
            "/api/jobs/" + created["id"],
            json={"mutex_group": "my-group"},
        )
        assert r.status_code == 200
        assert r.json()["job"]["mutex_group"] == "my-group"
        # Clear it (null).
        r = client.put(
            "/api/jobs/" + created["id"],
            json={"mutex_group": None},
        )
        assert r.status_code == 200
        assert "mutex_group" not in r.json()["job"]

    def test_invalid_mutex_group_rejected(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs", json={
            "name": "X", "script_path": "C:\\stub\\x.py",
            "mutex_group": "BAD CAPS",
        })
        assert resp.status_code == 400
