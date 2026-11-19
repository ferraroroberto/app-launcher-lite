"""Copilot's "Confirm folder trust" dialog: read off the screen, named on the
Board card, refused as input, and answered with Copilot's own "remember" option.

The dialog text is what Copilot CLI 1.0.114 paints in a folder it has not
trusted (probed live): a digit alone picks an option, and option 2 makes
Copilot add the folder to ``trustedFolders`` itself.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src import audit
from src.clients import session as session_client
from src.session_view import folder_trust

FOLDER = r"E:\work\unseen-project"

DIALOG = [
    "● No Copilot instructions found for this repository. Run /init to generate them.",
    "╭──────────────────────────────────────────────────────────────────────────────╮",
    "│ Confirm folder trust                                                         │",
    "│ ──────────────────────────────────────────────────────────────────────────── │",
    "│ ╭──────────────────────────────────────────────────────────────────────────╮ │",
    f"│ │ {FOLDER:<72} │ │",
    "│ ╰──────────────────────────────────────────────────────────────────────────╯ │",
    "│                                                                              │",
    "│ Copilot can read files in this folder and, with your permission, edit them.  │",
    "│                                                                              │",
    "│ Do you trust the files in this folder?                                       │",
    "│                                                                              │",
    "│ ❯ 1. Yes                                                                     │",
    "│   2. Yes, and remember this folder for future sessions                       │",
    "│   3. No (Esc)                                                                │",
    "│ ↑/↓ to navigate · enter to select · esc to cancel                            │",
    "╰──────────────────────────────────────────────────────────────────────────────╯",
]
PROMPT_SCREEN = [
    "❯ ",
    "← open sidebar · Interactive · Allow All · / commands",
]


def test_parse_prompt_names_the_folder_and_the_remember_key():
    prompt = folder_trust.parse_prompt(DIALOG)
    assert prompt == folder_trust.FolderTrustPrompt(folder=FOLDER, remember_key="2")
    assert folder_trust.answer_keys(prompt) == [("2", False)]
    assert folder_trust.folder_name(prompt.folder) == "unseen-project"


def test_a_screen_without_the_dialog_is_not_a_prompt():
    assert folder_trust.parse_prompt(PROMPT_SCREEN) is None
    assert folder_trust.parse_prompt([]) is None


def test_a_dialog_with_no_remember_option_is_never_answered_blind():
    reworded = [ln.replace("Yes, and remember this folder", "Yes, always") for ln in DIALOG]
    assert folder_trust.parse_prompt(reworded) is None


def test_the_remember_key_follows_the_label_not_a_fixed_digit():
    moved = [
        ln.replace("2. Yes, and remember", "3. Yes, and remember").replace("3. No (Esc)", "2. No (Esc)")
        for ln in DIALOG
    ]
    assert folder_trust.parse_prompt(moved).remember_key == "3"


# ---------------------------------------------------------------- the capture


def _write_capture(path, lines: List[str]) -> None:
    # Written the way the session-host writes it: text mode.
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\r\n".join(lines))


@pytest.fixture
def capture(monkeypatch, tmp_path):
    path = tmp_path / "s1.transcript"
    monkeypatch.setattr(audit, "transcript_path", lambda sid: path)
    return path


def _live(**over: Any) -> Dict[str, Any]:
    base = {"session_id": "s1", "kind": "pty", "agent": "copilot", "alive": True,
            "rows": 40, "cols": 100, "prompt_title": ""}
    base.update(over)
    return base


def test_pending_for_session_reads_the_dialog_off_the_capture(capture):
    _write_capture(capture, DIALOG)
    prompt = folder_trust.pending_for_session(_live())
    assert prompt is not None and prompt.folder == FOLDER


def test_a_trusted_folder_shows_no_dialog(capture):
    _write_capture(capture, PROMPT_SCREEN)
    assert folder_trust.pending_for_session(_live()) is None


def test_the_dialog_is_gone_once_copilot_repaints(capture):
    """The screen is replayed, so a dialog that was answered is not showing."""
    _write_capture(capture, DIALOG)
    with capture.open("a", encoding="utf-8") as fh:
        fh.write("\x1b[2J\x1b[H" + "\r\n".join(PROMPT_SCREEN))
    assert folder_trust.pending_for_session(_live()) is None


def test_copilots_private_status_query_does_not_break_the_render(capture):
    _write_capture(capture, ["\x1b[?996n", *DIALOG])
    assert folder_trust.pending_for_session(_live()) is not None


@pytest.mark.parametrize(
    "over",
    [
        {"agent": "claude"},
        {"kind": "remote"},
        {"alive": False},
        {"prompt_title": "say hi"},
        {"session_id": ""},
    ],
    ids=["other-agent", "detached", "dead", "already-prompted", "no-id"],
)
def test_only_a_live_copilot_that_has_not_taken_a_prompt_is_read(capture, over):
    _write_capture(capture, DIALOG)
    assert folder_trust.pending_for_session(_live(**over)) is None


# ----------------------------------------------------------------- the input


class _Host:
    def __init__(self, info):
        self.info = info
        self.posts: list = []

    def __call__(self, method, port, path, **kwargs):
        if method == "GET":
            return self.info
        self.posts.append((path, kwargs.get("json")))
        return {"delivered": True, "reason": "unverified"}


@pytest.fixture
def host(monkeypatch):
    import time

    h = _Host({
        "session_id": "s1", "kind": "pty", "agent": "copilot", "alive": True,
        "live_title": "GitHub Copilot", "started_at": time.time() - 30,
        "last_output_at": time.time() - 5, "rows": 40, "cols": 100, "prompt_title": "",
    })
    monkeypatch.setattr(session_client, "_request", h)
    return h


def test_input_into_an_untrusted_folder_is_refused_by_name(host, capture):
    _write_capture(capture, DIALOG)

    with pytest.raises(session_client.FolderTrustPending) as err:
        session_client.send_input(8466, "s1", "hello")

    assert err.value.status == 409
    assert err.value.needs_folder_trust is True
    assert FOLDER in str(err.value)
    assert "needs folder trust" in str(err.value)
    assert "NOT delivered" in str(err.value)
    assert host.posts == []


def test_input_into_a_trusted_folder_is_written(host, capture):
    _write_capture(capture, PROMPT_SCREEN)

    session_client.send_input(8466, "s1", "hello")

    assert host.posts == [("/sessions/s1/input", {"data": "hello", "submit": True})]


def test_the_same_input_delivers_once_the_folder_is_trusted(host, capture):
    """The launch's first message: refused while the dialog is up, written
    after the owner trusts the folder."""
    _write_capture(capture, DIALOG)
    with pytest.raises(session_client.FolderTrustPending):
        session_client.send_input(8466, "s1", "first message")
    assert host.posts == []

    _write_capture(capture, PROMPT_SCREEN)
    session_client.send_input(8466, "s1", "first message")

    assert host.posts == [("/sessions/s1/input", {"data": "first message", "submit": True})]


# ------------------------------------------------------------------- the route


@pytest.fixture
def trust_client(webapp_client, monkeypatch, capture):
    from app.webapp.routers import session_folder_trust as router
    from src import middleware

    monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    client, _, overrides = webapp_client
    session = overrides["session"]
    session.list_sessions.return_value = [_live()]
    monkeypatch.setattr(router, "_TRUST_POLL_S", 0)
    typed: List[Any] = []

    async def fake_pty(port, sid, keys):
        typed.append((sid, keys))
        # Copilot closes the dialog once the key lands.
        _write_capture(capture, PROMPT_SCREEN)

    monkeypatch.setattr(router, "_type_into_pty", fake_pty)
    return client, session, typed


def _post(client):
    return client.post("/api/claude-code/sessions/s1/folder-trust")


def test_the_trust_route_is_passkey_gated():
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/folder-trust") == "passkey"


def test_the_trust_route_picks_remember_and_reports_the_dialog_closed(trust_client, capture):
    client, _, typed = trust_client
    _write_capture(capture, DIALOG)

    res = _post(client)

    assert res.status_code == 200
    assert res.json() == {"ok": True, "folder": FOLDER, "trusted": True}
    assert typed == [("s1", [("2", False)])]


def test_the_trust_route_types_nothing_when_no_dialog_is_showing(trust_client, capture):
    client, _, typed = trust_client
    _write_capture(capture, PROMPT_SCREEN)

    res = _post(client)

    assert res.status_code == 409
    assert "not waiting on a folder-trust prompt" in res.json()["detail"]
    assert typed == []


def test_the_trust_route_409s_for_a_session_that_is_gone(trust_client):
    client, session, typed = trust_client
    session.list_sessions.return_value = []

    assert _post(client).status_code == 409
    assert typed == []


def test_the_board_card_names_the_trust_state(capture):
    from datetime import datetime, timezone

    from src.board import card_status
    from src.board.sessions import merge_sessions

    live = [{**_live(), "name": "unseen-project", "project_dir": FOLDER}]
    _write_capture(capture, DIALOG)
    cards = merge_sessions(live, [], now=datetime.now(timezone.utc))
    assert cards[0]["folder_trust"] == "unseen-project"
    assert cards[0]["status"] == card_status.AWAITING_DECISION

    _write_capture(capture, PROMPT_SCREEN)
    cards = merge_sessions(live, [], now=datetime.now(timezone.utc))
    assert cards[0]["folder_trust"] == ""
    assert cards[0]["status"] != card_status.AWAITING_DECISION
