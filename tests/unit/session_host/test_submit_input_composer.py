"""A host-sent submit leaves the composer known empty, a person's half-typed command does not."""

from __future__ import annotations

from src.session_host import PtySession
from src.session_host_composer import composer_tracker_for
from tests.unit.session_host._support import _make_session


def _copilot_session() -> PtySession:
    session = _make_session()
    session._composer = composer_tracker_for("copilot")
    assert session._composer is not None
    session._bracketed_paste_mode = True
    return session


def test_fresh_chief_slash_command_leaves_the_composer_known_empty(clock):
    session = _copilot_session()
    # The spawn's first typed line, the title rename, then the chief command.
    session.submit_input("/rename chief", True)
    assert session._composer.state()[0] is False

    outcome = session.submit_input("/chief", True)

    assert outcome.submitted is True
    assert session._composer.state() == (False, None)


def test_issue_mention_in_a_host_sent_message_leaves_the_composer_known_empty(clock):
    session = _copilot_session()

    session.submit_input("pick up #320", True)

    assert session._composer.state()[0] is False


def test_a_persons_half_typed_slash_command_still_reads_unknown(clock):
    session = _copilot_session()

    session._composer.feed("/chi")
    session._composer.feed("\r")

    assert session._composer.state()[0] is None


def test_host_send_onto_an_unknown_composer_stays_unknown(clock):
    session = _copilot_session()
    session._composer.mark_unknown()

    session.submit_input("/chief", True)

    assert session._composer.state()[0] is None


def test_host_send_without_a_submit_leaves_the_composer_dirty(clock):
    session = _copilot_session()

    session.submit_input("/chief", False)

    assert session._composer.state()[0] is True
