"""PtySession.submit_input against Copilot CLI's collapsed paste: the "[Paste #N - M lines]" chip as ingest evidence, and the post-Enter screen read that confirms the submit or reports it untaken."""

from __future__ import annotations

from src.session_host_input import (
    INPUT_NOT_INGESTED,
    INPUT_OK,
    INPUT_SUBMIT_UNTAKEN,
)
from src.vt_snapshot import VtSnapshot
from tests.unit.session_host._support import _echo, _FakeClock, _make_session

_PAYLOAD = "\n".join(f"line {i:02d} of a long steer for the agent" for i in range(16))
_INPUT_ROW = "\x1b[40;1H\x1b[2K❯ "
_CHIP = "[Paste #1 - 16 lines]"


def _copilot_session():
    session = _make_session()
    session._vt = VtSnapshot(40, 120)
    return session


def _paint(session, text: str, now: float) -> None:
    _echo(session, text)
    session._vt.feed(text)
    session._last_output_at = now


def _paste_then(session, clock: _FakeClock, enter_takes: int):
    """Copilot collapses the paste into a chip on its input line, then clears
    the line on the ``enter_takes``-th Enter (0: never)."""

    def _on_sleep(c: _FakeClock) -> None:
        if not session._ring:
            _paint(session, _INPUT_ROW + _CHIP, c.now)
        enters = [w.args[0] for w in session._pty.write.call_args_list].count("\r")
        if enter_takes and enters >= enter_takes and _CHIP in session._vt.screen_text():
            _paint(session, _INPUT_ROW, c.now)

    clock.on_sleep(_on_sleep)
    return session.submit_input(_PAYLOAD, True)


def test_copilot_paste_chip_counts_as_ingested_and_submits(clock):
    """The stranded-chip repro: a 16-line payload Copilot only ever shows as
    its chip used to read as never ingested, so Enter was withheld."""
    session = _copilot_session()

    outcome = _paste_then(session, clock, enter_takes=1)

    assert outcome.reason == INPUT_OK
    assert outcome.submit_confirmed is True
    assert outcome.delivered is True
    assert [w.args[0] for w in session._pty.write.call_args_list].count("\r") == 1


def test_an_enter_that_does_not_take_is_pressed_once_more(clock):
    session = _copilot_session()

    outcome = _paste_then(session, clock, enter_takes=2)

    assert outcome.reason == INPUT_OK
    assert outcome.submit_confirmed is True
    assert [w.args[0] for w in session._pty.write.call_args_list].count("\r") == 2


def test_a_chip_still_on_screen_after_two_enters_is_not_delivered(clock):
    session = _copilot_session()

    outcome = _paste_then(session, clock, enter_takes=0)

    assert outcome.reason == INPUT_SUBMIT_UNTAKEN
    assert outcome.delivered is False
    assert outcome.submit_state == "not_submitted"
    assert [w.args[0] for w in session._pty.write.call_args_list].count("\r") == 2


def test_an_agent_that_never_paints_the_chip_is_still_not_ingested(clock):
    session = _copilot_session()

    outcome = session.submit_input(_PAYLOAD, True)

    assert outcome.reason == INPUT_NOT_INGESTED
    assert "\r" not in [w.args[0] for w in session._pty.write.call_args_list]
