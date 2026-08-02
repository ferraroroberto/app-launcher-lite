"""Board drill-down (issue #301) — exchange parser, reply proxy, issue start.

Covers the act-from-the-card loop server-side:
  * ``board.last_exchange`` — tail JSONL parsing: text blocks joined across
    lines of the same assistant message, thinking/tool_use lines skipped,
    tool-result user lines skipped, harness wrappers skipped, missing file
    degraded to ``available: False``.
  * ``board.state_row_for_session`` — resolves the same row the board's
    merge renders (newest-session-wins claim order).
  * ``POST /api/coding/sessions/{sid}/input`` — one call to the
    session-host, which now owns framing + settle-then-submit itself
    (#611); the bare-submit escape hatch for a stranded composer.
  * ``POST /api/board/issues/start`` — server-built ``/issue-<mode> <N>``
    prompt, mode/number validation, repo resolution in the projects folder.
  * ``GET /api/board/sessions/{sid}/exchange`` — agent-aware native history,
    then the exact-id launcher capture fallback (#457).
  * passkey classification of all three new paths (gate refusal off-tailnet
    + ``_terminal_guard_level`` mapping).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import board, board_exchange


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ last_exchange


def _write_jsonl(path: Path, lines: list) -> str:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return str(path)


def _user_line(text) -> dict:
    return {
        "type": "user", "timestamp": "2026-07-02T11:50:00Z",
        "message": {"role": "user", "content": text},
    }


def _assistant_line(blocks: list, msg_id: str = "m1") -> dict:
    return {
        "type": "assistant", "timestamp": "2026-07-02T11:55:00Z",
        "message": {"id": msg_id, "role": "assistant", "content": blocks},
    }


def test_last_exchange_happy_path(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("fix the bug please"),
        _assistant_line([{"type": "thinking", "thinking": "hmm"}]),
        _assistant_line([{"type": "tool_use", "name": "Bash", "input": {}}]),
        _user_line([{"type": "tool_result", "content": "exit 0"}]),
        _assistant_line([{"type": "text", "text": "Done — the bug is fixed."}]),
    ])
    result = board.last_exchange(target)
    assert result["available"] is True
    assert result["assistant"]["text"] == "Done — the bug is fixed."
    assert result["user"]["text"] == "fix the bug please"


def test_last_exchange_joins_blocks_of_same_message(tmp_path: Path):
    """Transcripts write one line per content block — same message.id lines
    are one reply and must be joined in order."""
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("q"),
        _assistant_line([{"type": "text", "text": "First part."}], msg_id="m9"),
        _assistant_line([{"type": "tool_use", "name": "Read"}], msg_id="m9"),
        _assistant_line([{"type": "text", "text": "Second part."}], msg_id="m9"),
    ])
    result = board.last_exchange(target)
    assert result["assistant"]["text"] == "First part.\n\nSecond part."


def test_last_exchange_earlier_message_not_merged(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("q"),
        _assistant_line([{"type": "text", "text": "Old reply."}], msg_id="m1"),
        _user_line("follow-up"),
        _assistant_line([{"type": "text", "text": "New reply."}], msg_id="m2"),
    ])
    result = board.last_exchange(target)
    assert result["assistant"]["text"] == "New reply."
    assert result["user"]["text"] == "follow-up"


def test_last_exchange_skips_harness_wrapper_user_lines(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("the real prompt"),
        _user_line("<command-name>/compact</command-name>"),
        _assistant_line([{"type": "text", "text": "reply"}]),
    ])
    assert board.last_exchange(target)["user"]["text"] == "the real prompt"


def test_last_exchange_missing_or_empty():
    assert board.last_exchange(None)["available"] is False
    assert board.last_exchange("C:/nope/missing.jsonl")["available"] is False


def test_last_exchange_no_assistant_text_in_tail(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("q"),
        _assistant_line([{"type": "tool_use", "name": "Bash"}]),
    ])
    assert board.last_exchange(target)["available"] is False


# ------------------------------------------ agent-aware source fallbacks (#457)


def test_launcher_exchange_ignores_coloured_tool_block_and_reads_input_log(
    tmp_path: Path,
):
    capture = tmp_path / "s.transcript"
    capture.write_text(
        "\x1b[32m● Ran a tool command\r\n"
        "  noisy tool result\r\n"
        "\x1b[39m● The actual assistant answer\r\n"
        "  continues on this line.\r\n",
        encoding="utf-8",
    )
    input_log = tmp_path / "s.log"
    input_log.write_text(
        "2026-07-02T12:00:00 [input] '\\x1b[200~full prompt\\x1b[201~'\n"
        "2026-07-02T12:00:01 [input] '\\r'\n",
        encoding="utf-8",
    )
    result = board_exchange.launcher_last_exchange(
        capture, launcher_input_path=input_log, rows=20, cols=80
    )
    assert result["source"] == "launcher"
    assert result["user"]["text"] == "full prompt"
    assert result["assistant"]["text"] == (
        "The actual assistant answer continues on this line."
    )


def test_launcher_exchange_drops_grey_in_flight_tool_after_reply(tmp_path: Path):
    capture = tmp_path / "s.transcript"
    capture.write_text(
        "\x1b[38;2;255;255;255m● Completed assistant reply.\r\n"
        "\x1b[38;2;153;153;153m● Bash(pytest -q)\r\n"
        "  running…\r\n",
        encoding="utf-8",
    )
    result = board_exchange.launcher_last_exchange(
        capture, prompt_fallback="run the tests", rows=20, cols=80
    )
    assert result["assistant"]["text"] == "Completed assistant reply."


def test_launcher_exchange_new_session_is_true_empty(tmp_path: Path):
    capture = tmp_path / "new.transcript"
    capture.write_text("agent startup chrome only\r\n", encoding="utf-8")
    result = board_exchange.launcher_last_exchange(capture, rows=10, cols=40)
    assert result["available"] is False
    assert result["reason"] == "no_exchange"


def test_launcher_exchange_reads_only_the_bounded_capture_tail(
    tmp_path: Path, monkeypatch,
):
    capture = tmp_path / "bounded.transcript"
    capture.write_text(
        "● Old reply must fall outside the read window.\r\n"
        + ("x" * 200)
        + "\r\n● New reply is inside the bounded tail.\r\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(board_exchange, "_CAPTURE_TAIL_BYTES", 96)
    result = board_exchange.launcher_last_exchange(
        capture, prompt_fallback="latest?", rows=10, cols=80
    )
    assert result["assistant"]["text"] == "New reply is inside the bounded tail."


# --------------------------------- private-DSR resilience on the capture tail (#4)
# Byte-identical root cause to issue #2 (see test_vt_snapshot.py), but on the
# Board drill-down's request path: `_terminal_rows` used to build a bare
# `pyte.HistoryScreen`, so a private DSR (`ESC[?996n`, Copilot CLI 1.0.77's
# light/dark colour-scheme query) landing inside the captured tail raised
# `TypeError` straight out of `Stream.feed` and would 500 the endpoint.
_COPILOT_STARTUP_PRIVATE_CSI = (
    "\x1b[?1049h\x1b[?1004h\x1b[?2004h\x1b[?1003h\x1b[?1006h"
    "\x1b[?9001h\x1b[?25l\x1b[?996n\x1b[?u"
)


def test_terminal_rows_survives_private_dsr_in_capture_tail():
    """The exact Copilot 1.0.77 startup burst must not raise, and text on
    either side of it must still reach the parsed rows."""
    raw = (
        "before" + _COPILOT_STARTUP_PRIVATE_CSI + "\r\n\x1b[39m• after\r\n"
    )
    rows = board_exchange._terminal_rows(raw, rows=10, cols=40)
    plain = "\n".join(text for text, _marker in rows)
    assert "before" in plain
    assert "after" in plain


def test_launcher_exchange_survives_private_dsr_end_to_end(tmp_path: Path):
    """Same root cause exercised through the public entry point used by the
    Board drill-down endpoint."""
    capture = tmp_path / "s.transcript"
    capture.write_text(
        "\x1b[39m• before the DSR\r\n"
        + _COPILOT_STARTUP_PRIVATE_CSI
        + "\r\n\x1b[39m• after the DSR\r\n",
        encoding="utf-8",
    )
    result = board_exchange.launcher_last_exchange(capture, rows=20, cols=80)
    assert result["available"] is True
    assert "after the DSR" in result["assistant"]["text"]


# --------------------------------------------------- state_row_for_session


def _live_sess(session_id: str, project_dir: str, started_min_ago: int) -> dict:
    return {
        "session_id": session_id,
        "kind": "pty",
        "alive": True,
        "project_dir": project_dir,
        "started_at": _iso(NOW - timedelta(minutes=started_min_ago)),
    }


def test_state_row_for_session_matches_render_claim():
    """#537: an ambiguous single row (2 live sessions sharing one cwd)
    matches neither — state_row_for_session must stay consistent with what
    merge_sessions renders rather than resolving the ambiguity differently."""
    live = [_live_sess("old", "E:/a/x", 120), _live_sess("new", "E:/a/x", 5)]
    rows = {
        "t1": {"cwd": "E:/a/x", "status": "needs-you",
               "updated_at": _iso(NOW - timedelta(minutes=1)),
               "transcript_path": "p1"},
    }
    assert board.state_row_for_session(live, rows, "new") is None
    assert board.state_row_for_session(live, rows, "old") is None
    assert board.state_row_for_session(live, rows, "ghost") is None


# ----------------------------------------------------------- passkey gates


def test_new_paths_classified_passkey():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/coding/sessions/abc/input") == "passkey"
    assert _terminal_guard_level("/api/board/sessions/abc/exchange") == "passkey"
    assert _terminal_guard_level("/api/board/issues/start") == "passkey"


class TestGateRefusal:
    """The TestClient connects as host 'testclient' (not loopback, not
    tailnet) — all three #301 endpoints must be refused outright."""

    def test_input_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/coding/sessions/s1/input", json={"data": "hi"}
        )
        assert resp.status_code == 403

    def test_exchange_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/board/sessions/s1/exchange").status_code == 403

    def test_issue_start_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "x", "number": 1, "mode": "start"},
        )
        assert resp.status_code == 403


@pytest.fixture
def _bypass_gate(monkeypatch):
    """Treat the TestClient host as loopback so the gated proxy logic is
    exercised (the gate itself is covered by TestGateRefusal)."""
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware,
        "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


# ------------------------------------------------------------- reply proxy


class TestInputProxy:

    def test_multiline_forwarded_raw_in_one_call(self, webapp_client, _bypass_gate):
        """Framing + the submit CR are the session-host's own job now
        (#611) — the router just forwards data + submit in a single call."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "line one\nline two", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8466, "s1", "line one\nline two", True)

    def test_single_line_forwarded_raw(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "hello", "submit": True},
        )
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8466, "s1", "hello", True)

    def test_no_submit_forwards_submit_false(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "draft", "submit": False},
        )
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8466, "s1", "draft", False)

    def test_blank_data_without_submit_is_400(self, webapp_client, _bypass_gate):
        """Blank data with no submit is a genuine no-op request — nothing
        to write, nothing to submit — unlike the bare-submit escape hatch
        below, which always carries submit=True."""
        client, _, _ = webapp_client
        assert client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "   ", "submit": False},
        ).status_code == 400
        assert client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "", "submit": False},
        ).status_code == 400

    def test_bare_submit_escape_hatch_releases_stranded_composer(
        self, webapp_client, _bypass_gate
    ):
        """{"data": "", "submit": true} (#611) — release whatever is already
        sitting in the composer, with no text write. The recovery path for a
        message stranded by the submit race, previously only reachable by
        tapping the phone's own compose Send by hand."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8466, "s1", "", True)

    def test_whitespace_only_data_with_submit_is_bare_submit(
        self, webapp_client, _bypass_gate
    ):
        """Whitespace-only data collapses to the same bare-submit call as
        an empty string — there is no meaningful text to write either way."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "   ", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert calls[0].args == (8466, "s1", "", True)

    def test_dead_session_surfaces_as_error_not_false_ok(
        self, webapp_client, _bypass_gate
    ):
        """A session-host 409 (write dropped, issue #607) must propagate as a
        real error — never collapse back to {"ok": true}."""
        client, _, overrides = webapp_client
        overrides["session"].send_input.side_effect = (
            overrides["session"].SessionHostError(
                "session s1 not accepting input (exited)", status=409
            )
        )

        resp = client.post(
            "/api/coding/sessions/s1/input",
            json={"data": "hello", "submit": True},
        )

        assert resp.status_code == 409
        assert resp.json() != {"ok": True}


# ------------------------------------------------------------- issue start


class TestIssueStart:

    @pytest.fixture
    def _spawn(self, webapp_client, monkeypatch):
        from app.webapp.routers import board as board_router
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent, rows, cols,
                       history_lines=None):
            captured.update(
                project_dir=project_dir, name=name, flags=flags,
                port=port, kind=kind, agent=agent, rows=rows, cols=cols,
                history_lines=history_lines,
            )
            return {"session_id": "spawned-1", "kind": "pty", "name": name}

        monkeypatch.setattr(board_router, "spawn_agent_session", fake_spawn)
        # Every issue-start routes through the copilot install guard —
        # pretend the CLI is on PATH so tests don't depend on the host
        # machine. The not-installed test re-patches to False.
        from app.webapp.routers import board_spawn
        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda agent_id: True
        )
        return captured

    def test_builds_server_side_prompt(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "MyRepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert _spawn["flags"].endswith(' "/issue-start 42"')
        assert Path(_spawn["project_dir"]).name == "myrepo"
        assert _spawn["kind"] == "pty" and _spawn["agent"] == "copilot"
        assert resp.json()["session"]["session_id"] == "spawned-1"

    def test_yolo_mode(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "yolo"},
        )
        assert _spawn["flags"].endswith(' "/issue-yolo 7"')

    def test_rejects_bad_mode_and_number(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        base = {"repo": "myrepo", "number": 1, "mode": "start"}
        assert client.post(
            "/api/board/issues/start", json={**base, "mode": "add; rm -rf"}
        ).status_code == 400
        assert client.post(
            "/api/board/issues/start", json={**base, "number": "abc"}
        ).status_code == 400
        assert client.post(
            "/api/board/issues/start", json={**base, "number": -3}
        ).status_code == 400

    def test_always_launches_with_persisted_coding_model(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """The board model select is gone (Phase 6): every one-tap start
        launches with the persisted Coding model (gpt-5.6-luna in the
        default config); a stray ``model`` field from a stale-cache client
        is ignored, never applied."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 9, "mode": "start"},
        )
        assert "--model gpt-5.6-luna" in _spawn["flags"]
        assert _spawn["agent"] == "copilot"
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 9, "mode": "start",
                  "model": "gpt-5.6-sol"},
        )
        assert "--model gpt-5.6-luna" in _spawn["flags"]
        assert "gpt-5.6-sol" not in _spawn["flags"]

    def test_copilot_not_installed_400s(
        self, webapp_client, _bypass_gate, _spawn, monkeypatch
    ):
        from app.webapp.routers import board_spawn
        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda a: False
        )
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "start"},
        )
        assert resp.status_code == 400
        assert "not installed" in resp.json()["detail"]
        assert not _spawn

    def test_unknown_repo_is_404(self, webapp_client, _bypass_gate, _spawn):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "not-checked-out", "number": 1, "mode": "start"},
        )
        assert resp.status_code == 404

    def test_title_auto_names_the_spawned_session(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """#467: a Board start carrying the issue title renames the spawned
        session after it, via the #458 manual-override path (a launcher-side
        ``manual_title`` set — no PTY typing, so no readiness wait)."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "start",
                "title": "Board tab: auto-name a started session",
            },
        )
        assert resp.status_code == 200
        assert '--name "Board tab: auto-name a started session"' in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(
            8466, "spawned-1", "Board tab: auto-name a started session"
        )

    def test_unsafe_title_remains_launcher_only(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """A shell-sensitive issue title never reaches a native CLI flag."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        title = "docs & release"
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start", "title": title},
        )
        assert resp.status_code == 200
        assert "--name" not in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(8466, "spawned-1", title)

    def test_blank_title_skips_rename(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """No title (or whitespace-only) → no rename call; the session keeps
        its automatic title precedence."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start", "title": "   "},
        )
        assert resp.status_code == 200
        overrides["session"].rename.assert_not_called()

    def test_rename_failure_does_not_fail_the_launch(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """A rename error is best-effort — the launch still succeeds (#467)."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        overrides["session"].rename.side_effect = (
            overrides["session"].SessionHostError("boom", 502)
        )
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "yolo",
                "title": "some issue title",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["session"]["session_id"] == "spawned-1"


# -------------------------------------------------------- exchange endpoint


class TestExchangeEndpoint:

    def test_resolves_transcript_via_state_row(
        self, webapp_client, _bypass_gate, tmp_path: Path
    ):
        client, app, overrides = webapp_client
        transcript = _write_jsonl(tmp_path / "t.jsonl", [
            _user_line("status?"),
            _assistant_line([{"type": "text", "text": "All green."}]),
        ])
        state_file = Path(app.state.webapp_config.sessions_state_file)
        state_file.write_text(json.dumps({
            "t-uuid": {"cwd": "E:/proj/app", "status": "needs-you",
                       "updated_at": _iso(NOW), "transcript_path": transcript},
        }), encoding="utf-8")
        overrides["session"].list_sessions.return_value = [
            _live_sess("sess1", "E:/proj/app", 10)
        ]
        body = client.get("/api/board/sessions/sess1/exchange").json()
        assert body["available"] is True
        assert body["assistant"]["text"] == "All green."
        assert body["user"]["text"] == "status?"

    def test_unknown_session_degrades(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        body = client.get("/api/board/sessions/ghost/exchange").json()
        assert body["available"] is False
        assert body["reason"] == "session_not_found"

    @pytest.mark.parametrize("agent, with_missing_native", [
        ("copilot", True),
        ("ssh", False),
    ])
    def test_launcher_capture_falls_back_when_native_exchange_is_unavailable(
        self, webapp_client, _bypass_gate, tmp_path: Path, monkeypatch,
        agent: str, with_missing_native: bool,
    ):
        """#457 repro: both a missing hook transcript and a session with no
        hook row at all still have an exact-id launcher capture."""
        from app.webapp.routers import board as board_router

        client, app, overrides = webapp_client
        capture = tmp_path / "sess1.transcript"
        capture.write_text(
            "\x1b[39m\u2022 The exact launcher capture has the latest reply.\r\n"
            "  It remains linked by session id.\r\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            board_router.audit, "transcript_path", lambda _sid: capture
        )

        live = _live_sess("sess1", "E:/proj/app", 10)
        live.update(agent=agent, prompt_title="show the latest exchange")
        overrides["session"].list_sessions.return_value = [live]

        if with_missing_native:
            state_file = Path(app.state.webapp_config.sessions_state_file)
            state_file.write_text(json.dumps({
                "t-uuid": {
                    "agent": "copilot", "cwd": "E:/proj/app",
                    "status": "needs-you", "updated_at": _iso(NOW),
                    "transcript_path": str(tmp_path / "missing.jsonl"),
                },
            }), encoding="utf-8")

        body = client.get("/api/board/sessions/sess1/exchange").json()
        assert body["available"] is True
        assert body["source"] == "launcher"
        assert body["user"]["text"] == "show the latest exchange"
        assert body["assistant"]["text"] == (
            "The exact launcher capture has the latest reply. "
            "It remains linked by session id."
        )
