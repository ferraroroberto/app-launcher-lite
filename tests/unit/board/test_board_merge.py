"""Board tab — state-file reads and the live-session/state-row merge (issue #300 / #164)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from src import board
from tests.unit.board._board_helpers import (
    NOW,
    _iso,
    _live,
    _state_row,
    _transcript_file,
)

# ------------------------------------------------------ read_sessions_state


def test_state_missing_file_unavailable(tmp_path: Path):
    result = board.read_sessions_state(tmp_path / "nope.json", now=NOW)
    assert result == {"available": False, "stale": False, "updated_at": None, "rows": {}}


def test_state_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text("{not json", encoding="utf-8")
    assert board.read_sessions_state(target, now=NOW)["available"] is False


def test_state_fresh_rows(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text(json.dumps({
        "sid-1": {"project": "photo-ocr", "status": "needs-you",
                  "cwd": "E:/automation/photo-ocr",
                  "updated_at": _iso(NOW - timedelta(minutes=12))},
    }), encoding="utf-8")
    result = board.read_sessions_state(target, now=NOW)
    assert result["available"] is True
    assert result["stale"] is False
    assert set(result["rows"]) == {"sid-1"}


def test_state_stale_when_newest_row_old(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text(json.dumps({
        "sid-1": {"status": "working", "cwd": "x",
                  "updated_at": _iso(NOW - timedelta(hours=30))},
    }), encoding="utf-8")
    assert board.read_sessions_state(target, now=NOW)["stale"] is True


# ------------------------------------------------------ read_active_issues


def test_active_issues_missing_or_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "active-issues.json"
    assert board.read_active_issues(target, now=NOW) == {
        "available": False, "updated_at": None, "rows": {},
    }
    target.write_text("{not json", encoding="utf-8")
    assert board.read_active_issues(target, now=NOW)["available"] is False


def test_active_issues_keeps_fresh_and_expires_orphans(tmp_path: Path):
    target = tmp_path / "active-issues.json"
    target.write_text(json.dumps({
        "APP-LAUNCHER#528": {
            "repo": "app-launcher", "number": 528,
            "branch": "feat/528-active", "started_at": _iso(NOW - timedelta(hours=2)),
        },
        "photo-ocr#73": {
            "repo": "photo-ocr", "number": 73,
            "branch": "fix/73-old", "started_at": _iso(NOW - timedelta(hours=25)),
        },
        "broken#1": {"repo": "broken", "number": 1, "started_at": "garbage"},
    }), encoding="utf-8")

    result = board.read_active_issues(target, now=NOW)
    assert result["available"] is True
    assert result["updated_at"] == _iso(NOW - timedelta(hours=2))
    assert set(result["rows"]) == {"app-launcher#528"}


def test_merge_joins_by_normalized_cwd():
    cards = board.merge_sessions(
        [_live("aaa", "E:\\automation\\photo-ocr", 30)],
        {"t-uuid": _state_row("e:/automation/photo-ocr", status="needs-you")},
        now=NOW,
    )
    assert len(cards) == 1
    # #608: the raw hook "needs-you" is split; no transcript to check
    # pending tool_use against, so it lands on the safe generic fallback.
    assert cards[0]["status"] == "awaiting-input"
    assert cards[0]["project"] == "photo-ocr"
    assert cards[0]["session_id"] == "aaa"


def test_merge_carries_label_from_live_session():
    """The role tag (#245) rides the live session into the board card so the
    frontend can single out a purpose-built session; absent → empty string."""
    cards = board.merge_sessions(
        [
            _live("aaa", "E:\\automation\\fleet-config", 30, label="special"),
            _live("bbb", "E:\\automation\\photo-ocr", 20),
        ],
        {},
        now=NOW,
    )
    by_id = {c["session_id"]: c for c in cards}
    assert by_id["aaa"]["label"] == "special"
    assert by_id["bbb"]["label"] == ""


def test_merge_matches_cwd_under_project_dir():
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": _state_row("E:/automation/app-launcher/subdir", status="working")},
        now=NOW,
    )
    assert cards[0]["status"] == "working"


def test_merge_two_sessions_one_dir_ambiguous_row_stays_unclaimed():
    """#537: 2+ live sessions sharing one cwd can't be safely disambiguated
    by recency alone — the old greedy newest-wins tiebreak was exactly the
    mechanism that cross-wired live Board cards to the wrong session's
    transcript. Neither session claims the single row; both render
    ``unknown`` instead of one confidently (and possibly wrongly)
    inheriting it."""
    older = _live("old", "E:/automation/app-launcher", 120)
    newer = _live("new", "E:/automation/app-launcher", 5)
    cards = board.merge_sessions(
        [older, newer],
        {"t": _state_row("E:/automation/app-launcher", status="needs-you")},
        now=NOW,
    )
    by_id = {c["session_id"]: c for c in cards}
    assert by_id["new"]["status"] == "unknown"
    assert by_id["old"]["status"] == "unknown"


def test_merge_three_sessions_one_dir_never_cross_wires_transcripts():
    """#537 repro: 3 live sessions sharing one project directory, none
    carrying ``launcher_session_id`` (the exact-match path is unavailable —
    true for externally-started sessions, and for any launcher-spawned
    session predating the env-var propagation fix). Reproduced live against
    the real matching code: the old greedy recency tiebreak assigned every
    one of 3 such sessions a DIFFERENT session's transcript, all wrong
    simultaneously. No card may show another session's transcript_path —
    the safe outcome here is every card staying unmatched (``unknown``,
    no transcript), not a confident wrong guess."""
    sessions = [
        _live("s-oldest", "E:/automation/app-launcher", 60),
        _live("s-middle", "E:/automation/app-launcher", 40),
        _live("s-newest", "E:/automation/app-launcher", 20),
    ]
    rows = {
        "row-a": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=1, transcript_path="a.jsonl",
        ),
        "row-b": _state_row(
            "E:/automation/app-launcher", status="working",
            updated_min_ago=5, transcript_path="b.jsonl",
        ),
        "row-c": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=10, transcript_path="c.jsonl",
        ),
    }
    cards = board.merge_sessions(sessions, rows, now=NOW)
    by_id = {c["session_id"]: c for c in cards}
    assert len(by_id) == 3
    for sid in ("s-oldest", "s-middle", "s-newest"):
        assert by_id[sid]["status"] == "unknown"


def test_merge_other_agent_does_not_claim_agentless_row():
    """#455: agent-less rows default to the launcher's agent. A session
    running a different agent in the same cwd must stay truthful/unknown
    instead of borrowing that row's needs-you state."""
    other = _live(
        "ssh-live", "E:/automation/app-launcher", 5, agent="ssh"
    )
    cards = board.merge_sessions(
        [other],
        {"copilot-transcript": _state_row(
            "E:/automation/app-launcher", status="needs-you", updated_min_ago=240
        )},
        now=NOW,
    )
    assert len(cards) == 1
    assert cards[0]["session_id"] == "ssh-live"
    assert cards[0]["agent"] == "ssh"
    assert cards[0]["status"] == "unknown"
    assert cards[0]["state_sid"] is None


def test_merge_exact_launcher_id_beats_same_cwd_recency():
    """Agent writers can provide launcher_session_id for a deterministic
    claim; a newer sibling row in the same cwd must not steal the session."""
    live = [_live("launcher-aaa", "E:/automation/app-launcher", 30)]
    state_rows = {
        "right": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=20, agent="copilot",
            launcher_session_id="launcher-aaa",
        ),
        "newer-other": _state_row(
            "E:/automation/app-launcher", status="working",
            updated_min_ago=1, agent="copilot",
            launcher_session_id="launcher-bbb",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    assert len(cards) == 1
    # #608 split; see test_merge_joins_by_normalized_cwd.
    assert cards[0]["status"] == "awaiting-input"
    assert cards[0]["state_sid"] == "right"


def test_merge_duplicate_exact_launcher_ids_take_newest_row():
    """A resume/transcript rollover may leave more than one hook row carrying
    the inherited launcher id; exact identity still needs a deterministic
    newest-event tie-break."""
    live = [_live("launcher-aaa", "E:/automation/app-launcher", 30)]
    state_rows = {
        "older": _state_row(
            "E:/automation/app-launcher", status="working",
            updated_min_ago=20, agent="copilot",
            launcher_session_id="launcher-aaa",
        ),
        "newer": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=1, agent="copilot",
            launcher_session_id="launcher-aaa",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    # #608 split; see test_merge_joins_by_normalized_cwd.
    assert cards[0]["status"] == "awaiting-input"
    assert cards[0]["state_sid"] == "newer"


def test_merge_live_without_state_is_unknown():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["status"] == "unknown"
    assert cards[0]["project"] == "y"


def test_merge_bad_status_is_unknown():
    cards = board.merge_sessions(
        [_live("aaa", "E:/x/y", 10)],
        {"t": _state_row("E:/x/y", status="exploded")},
        now=NOW,
    )
    assert cards[0]["status"] == "unknown"


def test_merge_claimed_card_carries_state_sid():
    """#307: the card threads through the claimed row's own key, so a Slack
    ping (which only knows this transcript UUID) can resolve to the card."""
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30)],
        {"t-uuid": _state_row("E:/automation/photo-ocr", status="needs-you")},
        now=NOW,
    )
    assert cards[0]["state_sid"] == "t-uuid"


def test_merge_unclaimed_live_session_state_sid_is_none():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["state_sid"] is None


# ----------------------------------------- shared session title (#396)


def test_merge_carries_shared_name_from_state_row():
    """A matched state row's name/name_source (fleet-config#302) rides onto
    the card as shared_name/shared_name_source, alongside the existing
    live_title/prompt_title fields — the Coding tab reads the identical
    fields via board.attach_shared_names()."""
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30)],
        {"t": _state_row(
            "E:/automation/photo-ocr", status="needs-you",
            name="Fixing the chunk merge bug",
        )},
        now=NOW,
    )
    assert cards[0]["shared_name"] == "Fixing the chunk merge bug"
    assert cards[0]["shared_name_source"] is None


def test_merge_carries_shared_name_source_derived():
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30)],
        {"t": _state_row(
            "E:/automation/photo-ocr", status="working",
            name="photo-ocr-2", name_source="derived",
        )},
        now=NOW,
    )
    assert cards[0]["shared_name"] == "photo-ocr-2"
    assert cards[0]["shared_name_source"] == "derived"


def test_merge_no_state_row_shared_name_is_none():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["shared_name"] is None
    assert cards[0]["shared_name_source"] is None


def test_merge_cwd_fallback_ignores_row_older_than_session_start():
    """#482: a brand-new live session with no exact-id row yet must not
    inherit an unrelated leftover same-cwd row's title. The leftover here was
    last updated hours before this session even started, so it can only be
    some earlier, unrelated conversation in the same directory — a row can
    only be a session's own state if it was written at-or-after that
    session's started_at."""
    live = [_live("new-session", "E:/automation/app-launcher", 2)]
    state_rows = {
        "stale-leftover": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=300, name="Vendor project-scaffolding's button component",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    assert cards[0]["shared_name"] is None
    assert cards[0]["status"] == "unknown"
    assert cards[0]["state_sid"] is None


# ------------------------------------------ manual title override (#458)


def test_merge_carries_manual_title_from_live_session():
    """The session-host's manual_title (a launcher-native rename) rides onto
    the card alongside live_title/prompt_title — the Board drawer's rename
    button reads/writes the same field the Coding tab does."""
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30, manual_title="my rename")],
        {}, now=NOW,
    )
    assert cards[0]["manual_title"] == "my rename"


def test_merge_no_manual_title_defaults_empty_string():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["manual_title"] == ""


def test_merge_external_card_manual_title_is_empty_string(tmp_path: Path):
    """State-only (external) cards have no live session to hold an
    override — always empty, never None (matches live_title/prompt_title)."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards[0]["kind"] == "external"
    assert cards[0]["manual_title"] == ""


# --------------------------------------------------- attach_shared_names


def test_attach_shared_names_joins_by_cwd():
    live = [_live("aaa", "E:/automation/photo-ocr", 30)]
    state_rows = {"t": _state_row(
        "E:/automation/photo-ocr", name="Chunk merge fix",
    )}
    joined = board.attach_shared_names(live, state_rows)
    assert len(joined) == 1
    assert joined[0]["shared_name"] == "Chunk merge fix"
    assert joined[0]["shared_name_source"] is None
    # Every original field survives the join.
    assert joined[0]["session_id"] == "aaa"
    assert joined[0]["project_dir"] == "E:/automation/photo-ocr"


def test_attach_shared_names_no_match_returns_none():
    live = [_live("aaa", "E:/x/y", 10)]
    joined = board.attach_shared_names(live, {})
    assert joined[0]["shared_name"] is None
    assert joined[0]["shared_name_source"] is None


def test_attach_shared_names_does_not_cross_agent_boundary():
    live = [_live("ssh-live", "E:/automation/app-launcher", 10, agent="ssh")]
    state_rows = {"copilot-row": _state_row(
        "E:/automation/app-launcher", name="Copilot title",
    )}
    joined = board.attach_shared_names(live, state_rows)
    assert joined[0]["shared_name"] is None
    assert joined[0]["shared_name_source"] is None


def test_attach_shared_names_does_not_mutate_input():
    live = [_live("aaa", "E:/automation/photo-ocr", 30)]
    state_rows = {"t": _state_row("E:/automation/photo-ocr", name="X")}
    board.attach_shared_names(live, state_rows)
    assert "shared_name" not in live[0]


def test_attach_shared_names_agrees_with_merge_sessions():
    """The Coding tab and Board tab must resolve the same live session to the
    same title source — same cwd claim walk, same result (#396 acceptance)."""
    live = [_live("aaa", "E:/automation/photo-ocr", 30)]
    state_rows = {"t": _state_row(
        "E:/automation/photo-ocr", status="needs-you", name="Chunk merge fix",
    )}
    coding_tab = board.attach_shared_names(live, state_rows)
    board_tab = board.merge_sessions(live, state_rows, now=NOW)
    assert coding_tab[0]["shared_name"] == board_tab[0]["shared_name"]
    assert coding_tab[0]["shared_name_source"] == board_tab[0]["shared_name_source"]


def test_merge_unverifiable_state_only_row_is_suppressed():
    """#455: a hook row without a live host match or an existing, recently
    active transcript is not proof that a process exists."""
    cards = board.merge_sessions(
        [], {"t": _state_row("E:/automation/reporting", status="needs-you")}, now=NOW
    )
    assert cards == []


def test_merge_cold_state_only_row_dropped():
    cards = board.merge_sessions(
        [], {"t": _state_row("E:/x", updated_min_ago=60 * 30)}, now=NOW
    )
    assert cards == []


# ----------------------- external-state liveness (#322 / #455)


def test_merge_working_ghost_dropped(tmp_path: Path):
    """A headless/sdk-cli row stuck at 'working' with a long-quiet transcript
    is a dead process, not active work — dropped, not rendered."""
    row = _state_row("E:/automation/local-llm-hub", status="working", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=40))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_working_fresh_transcript_still_renders(tmp_path: Path):
    """A genuinely active 'working' row (recent transcript activity) still
    shows up — the ghost check must not catch real work in progress."""
    row = _state_row("E:/automation/local-llm-hub", status="working", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert len(cards) == 1
    assert cards[0]["status"] == "working"


def test_merge_working_no_transcript_path_is_suppressed():
    """#455: missing cloud/bridge transcripts are no longer trusted as
    external process-liveness evidence for up to 24 hours."""
    row = _state_row("E:/automation/local-llm-hub", status="working", updated_min_ago=40)
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_needs_you_quiet_transcript_is_suppressed(tmp_path: Path):
    """A waiting hook state is semantic evidence, not process-liveness
    evidence; once its external transcript is quiet, the card must clear."""
    row = _state_row("E:/automation/local-llm-hub", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=40))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


# ---------------------------------- ghost / dead-process cards (#613) -----


def test_merge_shared_transcript_superseded_row_does_not_double_render(tmp_path: Path):
    """#613 acceptance test: the fleet-config ghost, reproduced. A worker
    moving from one issue to the next gets its row re-keyed — the superseded
    row stays unmatched but keeps pointing at the transcript the live session
    keeps writing. One live session must yield exactly one card, never a
    real one plus a ghost carrying the old row's stale status."""
    transcript = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    live = [_live("aaa", "E:/automation/fleet-config", 30)]
    state_rows = {
        "current-row": _state_row(
            "E:/automation/fleet-config", status="working", updated_min_ago=1,
            transcript_path=transcript, launcher_session_id="aaa",
        ),
        "superseded-row": _state_row(
            "E:/automation/fleet-config", status="working", updated_min_ago=30,
            transcript_path=transcript, launcher_session_id="aaa",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    assert len(cards) == 1
    assert cards[0]["kind"] == "pty"
    assert cards[0]["session_id"] == "aaa"


def test_merge_reaped_launcher_session_suppressed_despite_fresh_transcript(tmp_path: Path):
    """#613: a hook row whose own ``launcher_session_id`` no longer appears
    in the live session-host list is provably dead (the session-host is the
    authority on which PTYs it owns) -- suppressed regardless of how recently
    its transcript happened to be touched. The Codex ghost's shape: a
    launcher-spawned session hard-killed without a SessionEnd, its row still
    inside the transcript-freshness window."""
    row = _state_row(
        "E:/automation/app-launcher", status="needs-you", updated_min_ago=5,
        transcript_path=_transcript_file(tmp_path, NOW - timedelta(minutes=1)),
        launcher_session_id="dead-pty-id",
    )
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_reaped_launcher_session_with_no_transcript_also_suppressed():
    row = _state_row(
        "E:/automation/app-launcher", status="working", updated_min_ago=2,
        launcher_session_id="dead-pty-id",
    )
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_reaped_other_agent_launcher_session_suppressed(tmp_path: Path):
    """#613's literal reported ghost, reproduced with a non-default agent:
    ``status=needs-you``, ~14.3 min old -- the exact shape from the issue.
    The reaped-session check is agent-agnostic (keyed only on
    ``launcher_session_id``, never on ``agent``) -- so this ghost is
    suppressed exactly like the default-agent case above, regardless of how
    fresh its transcript looks."""
    row = _state_row(
        "E:/automation/app-launcher", status="needs-you", updated_min_ago=14,
        transcript_path=_transcript_file(tmp_path, NOW - timedelta(minutes=1)),
        launcher_session_id="dead-ssh-pty-id", agent="ssh",
    )
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_external_row_liveness_reaped_check_does_not_fire_when_id_is_live(tmp_path: Path):
    """Unit-level check on ``_external_row_liveness`` directly: the
    reaped-session check must not fire for a row whose ``launcher_session_id``
    genuinely IS in the live set -- it falls through to the transcript-claim
    check and then the ordinary freshness fallback, exactly as before #613."""
    from src.board_transcript import _external_row_liveness

    row = _state_row(
        "E:/automation/app-launcher", status="working", updated_min_ago=2,
        transcript_path=_transcript_file(tmp_path, NOW - timedelta(minutes=1)),
        launcher_session_id="still-alive",
    )
    live_ok, reason = _external_row_liveness(
        row, NOW,
        claimed_transcripts=set(),
        live_launcher_session_ids={"still-alive"},
    )
    assert live_ok is True
    assert reason == "recent transcript activity"
