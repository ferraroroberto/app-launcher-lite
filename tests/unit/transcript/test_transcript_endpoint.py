"""Coding-tab transcript: the page and entry endpoints."""

from __future__ import annotations

import logging

import pytest

from src import board
from src.transcript_flavors import _shared as tf_shared
from tests.unit.transcript._support import (
    COPILOT_CAPTURE,
    _conversation,
    _live,
    _user,
    _write_jsonl,
)

# --------------------------------------------------------------- router


def test_transcript_path_classified_passkey():
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript").status_code == 403


class TestTranscriptEndpoint:

    def test_an_unreachable_session_host_is_not_a_missing_session(
        self, webapp_client, _bypass_gate
    ):
        """#1308: a list the host would not answer says nothing about the
        session, so it must not be reported as the terminal
        `session_not_found` the client latches off on."""
        from src.clients.session import SessionHostError

        client, _, overrides = webapp_client
        overrides["session"].list_sessions.side_effect = SessionHostError("timed out")
        for query in ("", "?after=0&size=0"):
            body = client.get(f"/api/claude-code/sessions/s1/transcript{query}").json()
            assert body["available"] is False, query
            assert body["reason"] == "session_host_unreachable", query
        entry = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert entry["reason"] == "session_host_unreachable"
        # Once the host answers again, an absent session is absent again.
        overrides["session"].list_sessions.side_effect = None
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["reason"] == "session_not_found"

    def test_unknown_session(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is False and body["reason"] == "session_not_found"
        assert body["entries"] == [] and body["next_cursor"] is None

    def test_detached_row_reads_its_claimed_history(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        # #966: a detached row has no PTY capture, but the reader never uses one.
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote")]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(2))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "copilot"
        assert "prompt 1 p" in [e["text"] for e in body["entries"] if e["kind"] == "user"]

    def test_copilot_row_reads_its_event_log(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """A Copilot session has no state row either — its hooks do not fire
        in the interactive TUI — so the route correlates its event log from
        the filesystem and reports its own source name."""
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="copilot")]
        monkeypatch.setattr(
            router_mod, "find_copilot_transcript", lambda session: COPILOT_CAPTURE,
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "copilot"
        # Copilot cannot mark every failure, and the page says so (#1020).
        assert body["tool_errors"] == "partial"
        assert [e["kind"] for e in body["entries"]][:3] == ["system", "user", "tool_call"]
        # The no-tools reply — the capture's last assistant belongs to
        # #1020's failing-view exchange, which is one short line.
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][2]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("Bounded file reading keeps log viewers")
        # An ambiguous correlation (two sessions launched in one folder inside
        # the window) is "no transcript", never a neighbour's conversation.
        monkeypatch.setattr(router_mod, "find_copilot_transcript", lambda session: None)
        assert client.get(
            "/api/claude-code/sessions/s1/transcript"
        ).json()["reason"] == "no_transcript"


    # `ssh` is the one registered agent that is not a coding harness, and
    # `claude` is an agent this fork cannot launch: neither has a reader here.
    @pytest.mark.parametrize("agent", ["ssh", "claude", "codex"])
    def test_an_agent_with_no_reader_is_unsupported(self, webapp_client, _bypass_gate, agent):
        client, _, overrides = webapp_client
        for kind in ("pty", "remote"):
            overrides["session"].list_sessions.return_value = [_live(kind=kind, agent=agent)]
            body = client.get("/api/claude-code/sessions/s1/transcript").json()
            assert body["reason"] == "unsupported_agent"

    def test_no_transcript_when_no_row_or_no_file(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(tmp_path / "gone.jsonl")},
        )
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"

    @pytest.mark.parametrize(
        ("failing_fn", "url"),
        [
            ("transcript_page", "/api/claude-code/sessions/s1/transcript"),
            ("entry_full_text", "/api/claude-code/sessions/s1/transcript/entry?offset=0"),
        ],
        ids=["page", "entry"],
    )
    def test_read_failure_is_distinct_from_missing(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, failing_fn, url
    ):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        def boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(router_mod, failing_fn, boom)
        body = client.get(url).json()
        assert body["available"] is False and body["reason"] == "read_failed"

    def test_happy_path_pages_and_never_logs_bodies(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(6, tool_calls_per_turn=1))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        with caplog.at_level(logging.INFO):
            first = client.get("/api/claude-code/sessions/s1/transcript?limit=6").json()
        assert first["available"] is True and first["source"] == "copilot"
        assert first["session_id"] == "s1" and first["reason"] is None
        assert [e["text"] for e in first["entries"] if e["kind"] == "user"] == ["prompt 4 pppp", "prompt 5 ppppp"]
        assert isinstance(first["next_cursor"], int)
        older = client.get(
            f"/api/claude-code/sessions/s1/transcript?limit=6&before={first['next_cursor']}"
        ).json()
        assert [e["text"] for e in older["entries"] if e["kind"] == "user"] == ["prompt 2 pp", "prompt 3 ppp"]
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "prompt 5" not in joined and "reply 5" not in joined
        assert "transcript s1" in joined  # the breadcrumb itself is there

    def test_limit_is_validated(self, webapp_client, _bypass_gate, monkeypatch):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=0").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=101").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?before=-1").status_code == 422


def test_transcript_entry_path_classified_passkey():
    """#985: a distinct guard-table row — '.../transcript/entry' does not
    end with '/transcript', so the sibling route's rule does not cover it
    for free (the exact omission class #997 found on the send route)."""
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript/entry") == "passkey"
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_entry_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").status_code == 403


class TestTranscriptEntryEndpoint:

    def test_offset_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript/entry").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=-1").status_code == 422

    def test_unknown_session_shares_the_page_routes_reasons(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert body["available"] is False and body["reason"] == "session_not_found"

    def test_happy_path_returns_uncapped_text_and_never_logs_body(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(tf_shared, "USER_TEXT_CAP", 5)
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("a much longer prompt than the cap")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        entry = page["entries"][0]
        assert entry["truncated"] is True

        with caplog.at_level(logging.INFO):
            body = client.get(
                f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
            ).json()
        assert body == {
            "available": True, "reason": None, "session_id": "s1",
            "text": "a much longer prompt than the cap", "truncated": False,
        }
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "a much longer prompt" not in joined
        assert "transcript entry s1" in joined

    def test_stale_offset_is_entry_not_found(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=9999").json()
        assert body["available"] is False and body["reason"] == "entry_not_found"
