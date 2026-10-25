"""Board tab — the transcript activity overlay, pending background dispatch, the needs-you split and the stalled threshold."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from src import board
from tests.unit.board._board_helpers import (
    NOW,
    _iso,
    _live,
    _msg_line,
    _state_row,
    _transcript_file,
)


def test_overlay_needs_you_with_active_transcript_is_working(tmp_path: Path):
    """Resume paths fire no hook (#305): a transcript appended well after the
    row's stamp means Claude is working, whatever the last hook event said."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
    # The age re-anchors to the transcript activity, not the stale hook stamp.
    assert cards[0]["age_seconds"] == 60


def test_overlay_idle_with_active_transcript_is_working(tmp_path: Path):
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_inside_stop_epsilon_keeps_needs_you(tmp_path: Path):
    """Stop's row stamp and the final transcript write land seconds apart (in
    either order) — inside the epsilon the hook status wins, so a genuine
    needs-you alert is never delayed. #608: the default fixture transcript
    is a plain text reply with nothing pending, so the split correctly reads
    it as a clean stop, not just an undifferentiated needs-you."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_live_title_is_busy_glyph_range():
    from src.board_transcript import _live_title_is_busy

    assert _live_title_is_busy("⠂ working") is True  # a real observed spinner frame
    assert _live_title_is_busy("⠐ Start work on issue 635") is True  # another frame
    assert _live_title_is_busy("✳ Some Conversation Title") is False  # idle marker
    assert _live_title_is_busy("") is False
    assert _live_title_is_busy(None) is False
    assert _live_title_is_busy("   ") is False
    assert _live_title_is_busy("app-launcher | gpt-5.5") is False  # Codex folder-echo title
    # The glyph must lead the title, not appear mid-string.
    assert _live_title_is_busy("Some Title ⠂") is False


def test_overlay_busy_live_title_overrides_stale_needs_you_inside_epsilon(tmp_path: Path):
    """#631: the classifier's tail-scan can land in the gap between a
    streamed text block and its own following tool_use block — landing
    inside the resume epsilon, with the transcript tail ending on a
    text-only assistant message, previously resolved to idle-finished even
    though a turn was genuinely still in progress. A busy live_title (the
    session's own OSC-title spinner glyph) is sourced from the live PTY, not
    this transcript file, so it short-circuits straight to working before
    the epsilon/tail-scan logic ever runs."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/x/y", 30, live_title="\u2802 doing the thing")],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "working"
    assert cards[0]["age_seconds"] == 0


def test_overlay_idle_live_title_does_not_force_a_false_override(tmp_path: Path):
    """The idle marker ("*", U+2733) must not be mistaken for the busy
    spinner range (U+2800-U+28FF) — same epsilon scenario as the busy-title
    test above, but with the idle glyph, must keep the original outcome."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/x/y", 30, live_title="\u2733 Some Conversation Title")],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "idle-finished"


# ------------------------------------------- wedged-PTY freshness gate (#636)


def test_pty_output_is_fresh_boundaries():
    from src.board_transcript import _WEDGED_PTY_AFTER, _pty_output_is_fresh

    assert _pty_output_is_fresh(None, NOW) is True  # no signal -> trust the glyph
    assert _pty_output_is_fresh(0.0, NOW) is True  # never recorded, same as no signal
    fresh = (NOW - timedelta(seconds=1)).timestamp()
    assert _pty_output_is_fresh(fresh, NOW) is True
    at_threshold = (NOW - _WEDGED_PTY_AFTER).timestamp()
    assert _pty_output_is_fresh(at_threshold, NOW) is True  # inclusive boundary
    stale = (NOW - _WEDGED_PTY_AFTER - timedelta(seconds=1)).timestamp()
    assert _pty_output_is_fresh(stale, NOW) is False


def test_overlay_busy_live_title_with_fresh_last_output_at_still_overrides(tmp_path: Path):
    """#636 must not regress #631: a busy live_title backed by genuinely
    recent PTY output (last_output_at inside the wedged-PTY threshold) keeps
    short-circuiting straight to working, same fixture as #631's own
    regression test."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(seconds=1)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "working"


def test_overlay_wedged_busy_live_title_does_not_force_working(tmp_path: Path):
    """A busy live_title frozen on a PTY that has gone genuinely quiet (#636,
    #627's remainder) must not be trusted at face value — same fixture as
    the #631 busy-title regression test above, but with a stale
    last_output_at, must fall through to the ordinary transcript-based
    outcome instead of blindly reporting working."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] != "working"
    assert cards[0]["status"] == "idle-finished"


def test_overlay_wedged_busy_live_title_falls_through_to_stalled_dispatch(tmp_path: Path):
    """A wedged busy live_title on top of a genuinely stalled background
    dispatch (#608) must resolve to stalled, not working — the freshness
    gate falling through has to reach the existing stalled-dispatch check,
    not just any transcript-based outcome. Same transcript fixture as
    test_stalled_background_dispatch_over_threshold_is_stalled."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 45,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "stalled"


def test_idle_finished_downgraded_when_repo_has_active_issue(tmp_path: Path):
    """#627: a clean stop with nothing pending is only an *absence* of
    evidence, not proof the session's own work is done — the same shape a
    turn cut off mid-task leaves behind. When the session's repo still has
    an open issue workflow (fleet-config's active-issue marker, already
    fetched every poll for the Backlog column), that ``idle-finished`` claim
    is downgraded to the safe ``awaiting-input`` rather than asserted from
    silence alone."""
    row = _state_row("E:/automation/app-launcher", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"app-launcher"},
    )
    assert cards[0]["status"] == "awaiting-input"


def test_idle_finished_stays_finished_without_matching_active_issue(tmp_path: Path):
    """The downgrade is repo-scoped, not a blanket suppression — a session in
    a repo with no open issue workflow (or none at all) still reports
    ``idle-finished`` as before."""
    row = _state_row("E:/automation/app-launcher", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"some-other-repo"},
    )
    assert cards[0]["status"] == "idle-finished"

    cards_no_repos = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)], {"t": row}, now=NOW,
    )
    assert cards_no_repos[0]["status"] == "idle-finished"


def test_idle_finished_downgrade_matches_worktree_sibling(tmp_path: Path):
    """A session built in an isolated sibling worktree (``<repo>-wt-<N>``,
    ``skills/_lib/worktree_claim.py``) must resolve to the same repo as its
    primary checkout, or its own still-open issue marker would never match."""
    row = _state_row(
        "E:/automation/app-launcher-wt-627", status="needs-you", updated_min_ago=10
    )
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher-wt-627", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"app-launcher"},
    )
    assert cards[0]["status"] == "awaiting-input"


def test_idle_finished_downgrade_applies_to_external_cards(tmp_path: Path):
    """The same repo-scoped downgrade applies to unmatched/external rows
    (:func:`src.board_sessions.merge_sessions`'s second, no-live-match loop),
    not just live-session-claimed cards. ``updated_min_ago=2`` keeps the
    transcript mtime inside both the #305 resume epsilon (so the status
    doesn't flip to ``working``) and #613's 5-minute external-liveness
    freshness window (so the row still renders as a card at all)."""
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=2)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=2) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [], {"t": row}, now=NOW, active_issue_repos={"reporting"},
    )
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "awaiting-input"


def test_active_issue_repos_lowercases_and_dedupes():
    rows = {
        "App-Launcher#627": {"repo": "App-Launcher", "number": 627},
        "app-launcher#628": {"repo": "app-launcher", "number": 628},
        "reporting#12": {"repo": "reporting", "number": 12},
        "bad-row": {"number": 1},
    }
    assert board.active_issue_repos(rows) == frozenset({"app-launcher", "reporting"})


def test_overlay_missing_transcript_keeps_hook_status(tmp_path: Path):
    """#608: a transcript path that can't be read gives no usable signal —
    the split must not misread that as "checked, nothing pending" and claim
    idle-finished; it falls back to the safe, generic value."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = str(tmp_path / "gone.jsonl")
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


def test_overlay_applies_to_external_cards(tmp_path: Path):
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "working"


def test_overlay_metadata_only_appends_keep_needs_you(tmp_path: Path):
    """#309: post-Stop metadata lines (system, pr-link, snapshots) advance the
    file mtime past the epsilon with no real resume — the tail probe sees the
    last conversation line is still pre-stamp and keeps the hook status.
    #608: the only real conversation line is a plain text reply with no
    tool_use pending, so the split correctly reads it as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _msg_line("assistant", stamp_time - timedelta(seconds=3)) + "\n"
        + json.dumps({"type": "system",
                      "timestamp": _iso(stamp_time + timedelta(minutes=2))}) + "\n"
        + json.dumps({"type": "pr-link", "url": "https://x"}) + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {"f": 1}}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), content
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"
    # Age still anchors to the hook stamp, not the metadata mtime.
    assert cards[0]["age_seconds"] == 600


def test_overlay_metadata_only_appends_keep_idle(tmp_path: Path):
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _msg_line("assistant", stamp_time - timedelta(seconds=3)) + "\n"
        + json.dumps({"type": "ai-title", "title": "t"}) + "\n"
    )
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), content
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle"


def test_overlay_message_buried_under_metadata_still_flips(tmp_path: Path):
    """A real resume followed by metadata lines: the reverse walk skips the
    metadata and finds the conversation line, so the flip still happens."""
    resumed = NOW - timedelta(minutes=1)
    content = (
        _msg_line("user", resumed) + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {}}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, resumed, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
    assert cards[0]["age_seconds"] == 60


def test_overlay_malformed_tail_keeps_hook_status(tmp_path: Path):
    """Unparseable tail (torn write, junk) degrades to the hook status. #608:
    no line parsed at all is "no usable signal", not "checked, clean" — the
    split must not read this as idle-finished."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), "{torn line no json\nnot json either\n"
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


# --------------------------------- pending background dispatch overlay (#464)


def _tool_result_line(ts: datetime, result: dict) -> str:
    """One tool_result line whose ``toolUseResult`` carries a background
    dispatch's synchronous launch ack — the shape ``_launched_background_ids``
    reads. ``toolUseResult`` is a sibling of ``message``, not inside it, per
    live transcripts."""
    return json.dumps({
        "type": "user",
        "timestamp": _iso(ts),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ack"}],
        },
        "toolUseResult": result,
    })


def _task_notification_line(ts: datetime, task_id: str) -> str:
    """A completion notice in Claude Code's real ``queue-operation`` shape —
    invisible to ``_last_activity`` since it carries no ``message`` field.
    Real transcripts pair the ``enqueue`` with an almost-immediate ``dequeue``
    once Claude actually receives it (#601); every caller here wants the
    fully-delivered case, so that pairing is baked in — the enqueue-only,
    still-queued case has its own dedicated fixture below."""
    enqueue = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": _iso(ts),
        "content": (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    })
    dequeue = json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": _iso(ts)})
    return enqueue + "\n" + dequeue


def test_overlay_pending_background_bash_keeps_working(tmp_path: Path):
    """A backgrounded Bash command Claude is still waiting on must not render
    as needs-you just because its own turn already ended — the parent
    transcript stays quiet the whole time it's running, so this must not
    depend on the mtime-past-stamp activity check."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        stamp_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_pending_background_agent_keeps_working(tmp_path: Path):
    """Same as above for an async sub-agent (Agent/Task tool) dispatch."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        stamp_time,
        {"isAsync": True, "status": "async_launched", "agentId": "agent1"},
    ) + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_pending_dispatch_survives_large_intervening_tool_result(tmp_path: Path):
    """#594: a background dispatch's launch line can be pushed well past the
    old 8 KiB activity window by one large intervening tool result (e.g. a
    file Read) before the turn ends — reproduced from a live transcript where
    the launch line sat 11.8 KB from EOF. The wider ``_EXCHANGE_TAIL_BYTES``
    scan must still reach it."""
    stamp_time = NOW - timedelta(minutes=10)
    launch = _tool_result_line(
        stamp_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    # Padding well past the old 8 KiB window but inside the new 256 KiB one.
    filler = (json.dumps({"type": "system", "content": "x" * 400}) + "\n") * 30
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, launch + filler)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def _turn_duration_line(ts: datetime, pending_agent_count: int) -> str:
    """Claude Code's own end-of-turn accounting line, emitted the moment a
    turn ends — the harness-native signal
    :func:`src.board_transcript._pending_background_dispatch_launched_at`
    falls back to when its id-keyed scan comes up empty (#627)."""
    return json.dumps({
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 1000,
        "pendingBackgroundAgentCount": pending_agent_count,
        "timestamp": _iso(ts),
    })


def test_overlay_pending_dispatch_beyond_tail_window_uses_turn_duration_count(tmp_path: Path):
    """#627, reproduced from a live transcript (project-scaffolding#199's
    worker): a background ``Agent`` dispatch's launch line sat ~299 KB from
    EOF by the time its turn ended — past even the widened 256 KiB
    ``_EXCHANGE_TAIL_BYTES`` window (#594 already widened it once, from 8 KB,
    for the same class of problem). The id-keyed scan below found nothing
    outstanding, so the row read as a clean stop (``idle-finished``) while the
    dispatch was still genuinely running — exactly the false-negative #627
    warns is worse than the five prior false-``needs-you`` reports it
    replaced. Claude Code's own ``turn_duration`` line reports
    ``pendingBackgroundAgentCount`` independent of any byte window (the real
    transcript's matching line read ``1`` at the same instant); the scan must
    fall back to it when the id-keyed search finds nothing."""
    stamp_time = NOW - timedelta(minutes=10)
    launch = _tool_result_line(
        stamp_time, {"isAsync": True, "status": "async_launched", "agentId": "agent1"}
    ) + "\n"
    # Padding well past the 256 KiB _EXCHANGE_TAIL_BYTES window.
    filler = (json.dumps({"type": "system", "content": "x" * 400}) + "\n") * 700
    final_text = _msg_line("assistant", stamp_time) + "\n"
    turn_duration = _turn_duration_line(stamp_time, 1) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, stamp_time, launch + filler + final_text + turn_duration
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_turn_duration_zero_pending_does_not_block_idle_finished(tmp_path: Path):
    """#627: a ``turn_duration`` line reporting zero pending agents is not
    itself evidence of outstanding work — a genuinely clean stop must still
    read as idle-finished, turn_duration line or not."""
    stamp_time = NOW - timedelta(minutes=10)
    final_text = _msg_line("assistant", stamp_time) + "\n"
    turn_duration = _turn_duration_line(stamp_time, 0) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, stamp_time, final_text + turn_duration
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_overlay_background_dispatch_notified_keeps_needs_you(tmp_path: Path):
    """Once the completion notice lands, a genuine needs-you-family status
    still wins — the pending check must not keep matching after the work
    actually finished. #608: nothing else is pending in this fixture, so the
    split reads it as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_result_line(
            stamp_time - timedelta(minutes=1),
            {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        ) + "\n"
        + _task_notification_line(stamp_time - timedelta(seconds=30), "btask1") + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_overlay_background_dispatch_enqueued_not_dequeued_keeps_working(tmp_path: Path):
    """#601: a live transcript (app-launcher#601, home-automation issue #321)
    ended with the notification's ``enqueue`` line as the very last thing
    written — no ``dequeue``/``remove`` followed it anywhere in the tail, so
    Claude had not actually received the result yet. The notification being
    merely *ready* (enqueued) must not resolve the dispatch — only being
    handed to Claude (dequeued) may."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_result_line(
            stamp_time - timedelta(minutes=1),
            {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        ) + "\n"
        + json.dumps({
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": _iso(stamp_time - timedelta(seconds=30)),
            "content": (
                "<task-notification>\n<task-id>btask1</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        }) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


# ------------------------ pending background dispatch, tool-use-id keyed (#576)


def _bash_tool_use_line(
    ts: datetime, tool_use_id: str, *, run_in_background: bool = True, tool_name: str = "Bash",
) -> str:
    """One assistant line dispatching a ``Bash`` (or, per #594, ``PowerShell``)
    tool call — the shape ``_launched_bash_dispatch_ids`` reads. This is the
    tool-call-id-keyed counterpart to ``_tool_result_line``'s
    ``toolUseResult``-keyed shape: #576 found real transcripts no longer
    carry ``backgroundTaskId`` on the result, so the launch has to be read
    off the ``tool_use`` block itself."""
    return json.dumps({
        "type": "assistant",
        "timestamp": _iso(ts),
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": {"command": "pwsh -File scripts/verify-before-ship.ps1",
                           "run_in_background": run_in_background},
            }],
        },
    })


def _tool_use_notification_line(ts: datetime, task_id: str, tool_use_id: str) -> str:
    """A completion notice carrying both correlation tags real transcripts
    use — ``<task-id>`` (the legacy scheme) and ``<tool-use-id>`` (#576).
    Paired with an immediate ``dequeue``, matching real delivery (#601) —
    see :func:`_task_notification_line`."""
    enqueue = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": _iso(ts),
        "content": (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    })
    dequeue = json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": _iso(ts)})
    return enqueue + "\n" + dequeue


def test_overlay_pending_bash_dispatch_without_background_task_id_keeps_working(tmp_path: Path):
    """A backgrounded Bash dispatch whose synchronous ack carries no
    ``backgroundTaskId`` (the real-world drift #576 found) must still be
    caught via the tool_use's own id, not just the legacy toolUseResult key."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(stamp_time, "toolu_abc") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_pending_powershell_dispatch_keeps_working(tmp_path: Path):
    """#594: this repo's own agents background long-running commands (e.g.
    the verify-before-ship gate) via the ``PowerShell`` tool, not ``Bash`` —
    the tool-call-id-keyed scheme must recognize it too, not just Bash."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(stamp_time, "toolu_ps1", tool_name="PowerShell") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_bash_dispatch_ack_alone_does_not_resolve_it(tmp_path: Path):
    """The synchronous 'launched in background' ack is an ordinary
    tool_result for the same tool_use_id — it must not be mistaken for
    completion, or the check would resolve the dispatch the instant it
    fires, defeating the whole point."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _bash_tool_use_line(stamp_time - timedelta(seconds=5), "toolu_abc") + "\n"
        + _tool_result_line(stamp_time, {"tool_use_id": "toolu_abc",
                                          "stdout": "Command running in background"}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_bash_dispatch_notified_by_tool_use_id_keeps_needs_you(tmp_path: Path):
    """Once the notification's ``<tool-use-id>`` tag resolves the dispatch,
    a genuine needs-you-family status still wins — mirrors the legacy
    scheme's own already-notified test, for the new id space. #608: the
    notification isn't a ``tool_result`` block, so the generic split's own
    scanner still sees the Bash call as unresolved — the safe generic
    fallback, not a false idle-finished claim (a materially different,
    already-notified-and-genuinely-resolved concern, which is what keeps the
    dispatch itself out of ``working``/``stalled`` here)."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _bash_tool_use_line(stamp_time - timedelta(minutes=1), "toolu_abc") + "\n"
        + _tool_use_notification_line(
            stamp_time - timedelta(seconds=30), "btask1", "toolu_abc"
        ) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


def test_overlay_foreground_bash_tool_use_is_not_flagged_pending(tmp_path: Path):
    """A plain (non-backgrounded) Bash call must not trip the new check —
    only ``run_in_background: true`` marks a dispatch as pending. #608: the
    fixture never adds a resolving tool_result (out of scope for what this
    test targets), so the generic split's own scanner still sees it
    unresolved and falls back to the safe generic value rather than
    misreading an incomplete fixture as a confirmed clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(
        stamp_time, "toolu_abc", run_in_background=False
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


# --------------------------------------- needs-you four-way split (#608)


def _tool_use_line(ts: datetime, tool_use_id: str, name: str) -> str:
    """One assistant line dispatching an arbitrary foreground tool call —
    the generic counterpart to :func:`_bash_tool_use_line`, for exercising
    :func:`src.board_transcript._refine_waiting_status`'s own tail scan
    rather than the background-dispatch-specific one."""
    return json.dumps({
        "type": "assistant",
        "timestamp": _iso(ts),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}],
        },
    })


def _generic_tool_result_line(ts: datetime, tool_use_id: str) -> str:
    """A plain, ordinary ``tool_result`` block for ``tool_use_id`` — the
    standard Anthropic content-block shape (no ``toolUseResult`` sibling),
    resolving whatever :func:`_tool_use_line` launched."""
    return json.dumps({
        "type": "user",
        "timestamp": _iso(ts),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
        },
    })


def test_refine_pending_ask_user_question_is_awaiting_decision(tmp_path: Path):
    """A pending AskUserQuestion — Claude is blocked on a human picking an
    option, not just "needs a prompt"."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_q1", "AskUserQuestion") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"


def test_refine_pending_exit_plan_mode_is_awaiting_decision(tmp_path: Path):
    """A pending ExitPlanMode is the same kind of human-decision block as
    AskUserQuestion — approve or revise a plan."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_p1", "ExitPlanMode") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"


def test_refine_resolved_ask_user_question_is_idle_finished(tmp_path: Path):
    """Once the decision tool_use resolves (a real tool_result lands), the
    session isn't blocked on a decision anymore — nothing else is pending,
    so it reads as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_use_line(stamp_time - timedelta(seconds=5), "toolu_q1", "AskUserQuestion") + "\n"
        + _generic_tool_result_line(stamp_time, "toolu_q1") + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_refine_pending_unrecognized_tool_use_is_awaiting_input(tmp_path: Path):
    """A pending tool_use that isn't a decision tool and isn't a background
    dispatch (e.g. a permission-gated Read waiting on approval) falls back
    to the generic, safe awaiting-input value — real, just not specifically
    classifiable."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_r1", "Read") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


def test_refine_only_applies_to_needs_you_not_idle(tmp_path: Path):
    """The split is scoped to needs-you — a plain ``idle`` row with a pending
    decision tool_use must not be reclassified; idle isn't part of #608."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_q1", "AskUserQuestion") + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle"


# --------------------------------------------- stalled threshold (#608)


def test_stalled_background_dispatch_under_threshold_stays_working(tmp_path: Path):
    """A background dispatch outstanding 10 minutes is healthy waiting, not
    a stall — mirrors an observed ~9-minute e2e-gate wait
    that must never misread as stalled."""
    stamp_time = NOW - timedelta(minutes=25)
    launch_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=25)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_stalled_background_dispatch_over_threshold_is_stalled(tmp_path: Path):
    """A background dispatch outstanding 35 minutes (past the 30-minute
    threshold) is a genuine anomaly worth surfacing distinctly from healthy
    waiting."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "stalled"


def test_stalled_dispatch_with_unparseable_launch_stamp_stays_working(tmp_path: Path):
    """A dispatch with no parseable launch timestamp is genuinely
    outstanding but not age-able — per explicit direction, a low-confidence
    stalled call is worse than a late one, so this must not be guessed as
    stalled just because the sentinel age looks ancient."""
    stamp_time = NOW - timedelta(minutes=40)
    content = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ack"}],
        },
        "toolUseResult": {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        # No "timestamp" field at all — _parse_iso(None) is None.
    }) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_stalled_status_stays_stalled_for_idle_hook_status(tmp_path: Path):
    """The stalled check only fires for a ``needs-you`` row — an ``idle``
    row with the same outstanding dispatch keeps the #464 pre-#608
    behavior (always ``working``), since idle is out of the four-way
    split's scope."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
