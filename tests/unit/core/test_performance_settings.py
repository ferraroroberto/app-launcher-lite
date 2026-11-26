"""Settings > Performance: the knob table, the presets, validation and live pool resizing."""

from __future__ import annotations

import json
import threading
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import performance_health, request_timing
from src.webapp_config import WebappConfig, load_webapp_config, update_webapp_config
from src.webapp_settings import performance as perf


def test_every_limit_is_a_config_field_with_the_table_default():
    names = {f.name for f in fields(WebappConfig)}
    defaults = WebappConfig()
    for knob in perf.KNOBS:
        assert knob.key in names, knob.key
        assert getattr(defaults, knob.key) == knob.default, knob.key


def test_the_sample_config_seeds_every_limit_at_its_default():
    # A fresh install copies the sample to the real config, so its values are what ships.
    path = Path(__file__).resolve().parents[3] / "config" / "webapp_config.sample.json"
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert {key: sample.get(key) for key in perf.KEYS} == perf.STANDARD


@pytest.mark.parametrize("name", [perf.PRESET_STANDARD, perf.PRESET_LIGHT])
def test_each_preset_is_inside_the_hard_limits(name):
    assert perf.problems(perf.PRESETS[name]) == []
    assert set(perf.PRESETS[name]) == set(perf.KEYS)


def test_light_machine_is_slower_and_smaller_than_standard():
    for knob in perf.KNOBS:
        if knob.kind == perf.KIND_BOOL or knob.auto_zero:
            continue
        std, light = knob.default, knob.light
        if knob.group == perf.GROUP_POLLS or knob.key in ("git_status_ttl_s", "transcript_resolve_miss_ttl_s", "request_budget_scale"):
            assert light >= std, knob.key
        else:
            assert light <= std, knob.key
    assert perf.LIGHT["pool_sends"] > 0, "Light names a size where Standard picks automatically"


def test_a_preset_is_detected_and_a_mix_is_custom():
    assert perf.detect_preset(perf.STANDARD) == perf.PRESET_STANDARD
    assert perf.detect_preset(perf.LIGHT) == perf.PRESET_LIGHT
    mixed = {**perf.LIGHT, "poll_board_s": 7}
    assert perf.detect_preset(mixed) == perf.PRESET_CUSTOM
    assert perf.payload(WebappConfig())["preset"] == perf.PRESET_STANDARD


@pytest.mark.parametrize(
    "key, value, words",
    [
        ("poll_board_s", 0, "between 2 and 120"),
        ("poll_board_s", 2.5, "whole number"),
        ("poll_board_s", True, "whole number"),
        ("poll_board_s", "5", "whole number"),
        ("pool_hot_poll", 3, "between 4 and 32"),
        ("pool_sends", 2, "or 0 for automatic"),
        ("git_status_ttl_s", 61, "between 2 and 60"),
        ("request_budget_scale", 0.1, "between 0.5 and 10"),
        ("poll_pause_hidden", 1, "on or off"),
    ],
)
def test_a_bad_value_is_refused_with_the_rule_it_broke(key, value, words):
    out = perf.problems({key: value})
    assert len(out) == 1 and words in out[0], out
    assert perf.KNOBS[perf.KEYS.index(key)].label in out[0]


def test_automatic_pool_size_zero_is_allowed():
    assert perf.problems({"pool_sends": 0}) == []


def test_a_save_out_of_range_is_refused_and_leaves_the_file_alone(webapp_client):
    _, app, overrides = webapp_client
    path: Path = overrides["tmp_webapp_cfg_path"]
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    with pytest.raises(ValueError, match="Board refresh must be a whole number between 2 and 120"):
        update_webapp_config(poll_board_s=1)
    assert (path.read_text(encoding="utf-8") if path.exists() else "") == before


def test_a_hand_edited_value_out_of_range_loads_as_its_default(tmp_path, caplog):
    path = tmp_path / "webapp_config.json"
    path.write_text(json.dumps({"poll_board_s": 0, "pool_git_status": 99, "pool_sends": 12}), encoding="utf-8")
    with caplog.at_level("WARNING"):
        cfg = load_webapp_config(path)
    assert cfg.poll_board_s == perf.default("poll_board_s")
    assert cfg.pool_git_status == perf.default("pool_git_status")
    assert cfg.pool_sends == 12, "a valid neighbour is kept"
    assert "poll_board_s reset to its default" in caplog.text


def test_a_float_limit_loads_as_a_float(tmp_path):
    path = tmp_path / "webapp_config.json"
    path.write_text(json.dumps({"transcript_resolve_miss_ttl_s": "20", "request_budget_scale": 2}), encoding="utf-8")
    cfg = load_webapp_config(path)
    assert cfg.transcript_resolve_miss_ttl_s == 20.0 and isinstance(cfg.transcript_resolve_miss_ttl_s, float)
    assert cfg.request_budget_scale == 2.0 and isinstance(cfg.request_budget_scale, float)


# ------------------------------------------------------------ live pool size


def test_a_pool_resize_changes_how_many_calls_run_at_once():
    pool = request_timing.TimedExecutor("probe", max_workers=2, ceiling=6)
    started: list[int] = []
    lock = threading.Lock()
    two_started, third_started, all_started = threading.Event(), threading.Event(), threading.Event()
    release = threading.Event()

    def job(n: int) -> None:
        with lock:
            started.append(n)
            if len(started) == 2:
                two_started.set()
            if len(started) == 3:
                third_started.set()
            if len(started) == 4:
                all_started.set()
        release.wait(timeout=10)

    futures = [pool.submit(job, n) for n in range(4)]
    try:
        assert two_started.wait(timeout=5)
        with lock:
            assert len(started) == 2, "a third call ran past the limit of 2"
        pool.resize(3)
        assert third_started.wait(timeout=5)
        with lock:
            assert len(started) == 3
        assert pool.resize(99) == 6, "the thread ceiling caps a resize"
        assert all_started.wait(timeout=5)
    finally:
        release.set()
        for f in futures:
            f.result(timeout=5)
        pool.shutdown()
    assert pool.limit == 6


def test_a_smaller_limit_holds_new_calls_until_running_ones_end():
    pool = request_timing.TimedExecutor("probe", max_workers=4, ceiling=4)
    running, release = threading.Semaphore(0), threading.Event()
    later = threading.Event()

    def hold() -> None:
        running.release()
        release.wait(timeout=10)

    first = [pool.submit(hold) for _ in range(4)]
    for _ in range(4):
        assert running.acquire(timeout=5)
    pool.resize(1)
    after = pool.submit(later.set)
    try:
        assert not later.wait(timeout=0.2), "a call started while 4 were still over the new limit of 1"
    finally:
        release.set()
    assert later.wait(timeout=5)
    for f in first + [after]:
        f.result(timeout=5)
    pool.shutdown()


def test_the_default_executor_resizes_and_zero_means_automatic():
    pool = request_timing.default_executor()
    try:
        assert pool.limit == request_timing.automatic_default_size()
        assert request_timing.resize_default_executor(9) == 9
        assert request_timing.resize_default_executor(0) == request_timing.automatic_default_size()
    finally:
        pool.shutdown()


def test_the_budget_scale_multiplies_defaults_and_overrides():
    assert request_timing.scaled_overrides({"board": [1.0, 2.0]}, 1) == {"board": [1.0, 2.0]}
    scaled = request_timing.resolve_budgets(request_timing.scaled_overrides({"board": [1.0, 2.0]}, 2.0))
    assert (scaled["board"].soft_s, scaled["board"].hard_s) == (2.0, 4.0)
    default_tick = request_timing.default_budgets()["transcript-tick"]
    assert scaled["transcript-tick"].hard_s == default_tick.hard_s * 2.0


# ------------------------------------------------------------- health line


def test_the_lane_count_is_live_managed_lanes_and_unknown_when_unreadable(tmp_path):
    managed, state = tmp_path / "managed.json", tmp_path / "state.json"
    managed.write_text(json.dumps({"s1": {}, "s2": {}, "s3": {}}), encoding="utf-8")
    state.write_text(
        json.dumps({
            "c1": {"launcher_session_id": "s1", "updated_at": "2026-11-22T10:00:00Z"},
            "c2": {"launcher_session_id": "s2", "updated_at": "2026-11-22T10:00:00Z", "status": "ended"},
            "c9": {"launcher_session_id": "other", "updated_at": "2026-11-22T10:00:00Z"},
        }),
        encoding="utf-8",
    )
    assert performance_health.lane_count(managed, state) == (1, "")
    managed.write_text("{not json", encoding="utf-8")
    count, why = performance_health.lane_count(managed, state)
    assert count is None and "lane file" in why
    managed.write_text("{}", encoding="utf-8")
    count, why = performance_health.lane_count(managed, tmp_path / "missing.json")
    assert count is None and "session state" in why


def test_health_says_unknown_for_a_figure_it_could_not_read(tmp_path, monkeypatch):
    from src import system_load

    system_load.reset()
    monkeypatch.setattr(performance_health, "agent_processes", lambda: None)
    cfg = SimpleNamespace(
        chief_managed_file=str(tmp_path / "managed.json"),
        sessions_state_file=str(tmp_path / "missing.json"),
        chief_worker_cap=3,
    )
    out = performance_health.read(cfg)
    assert out["cpu_percent"] is None and out["memory_free_gb"] is None
    assert {"cpu", "memory", "agents", "lanes"} <= set(out["unknown"])
    assert out["agent_processes"] is None and out["lanes"] is None
    assert out["worker_cap"] == 3
