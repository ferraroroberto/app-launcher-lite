"""The Board drawer's exchange-pane state machine, driven through the real JS module.

`app/webapp/static/board-exchange-state.js` has no imports, so Node loads it as-is.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_MODULE = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static" / "board-exchange-state.js"
_NODE = shutil.which("node")
_RUN_TIMEOUT_S = 30

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not installed")

_HARNESS = """
import { ExchangeState, deriveExchangeState, setExchangeState } from %s;
const fresh = () => ({ dataset: {} });
const out = {};
out.derived = [
  deriveExchangeState({ available: true }),
  deriveExchangeState({ available: false, reason: 'no_exchange' }),
  deriveExchangeState({ available: false, reason: 'session_not_found' }),
  deriveExchangeState({ available: false }),
];
const refused = (fn) => { try { fn(); return false; } catch (e) { return true; } };
const a = fresh();
out.settleBeforeLoading = refused(() => setExchangeState(a, ExchangeState.READY));
const b = fresh();
setExchangeState(b, ExchangeState.LOADING);
setExchangeState(b, ExchangeState.EMPTY);
out.afterEmpty = b.dataset.state;
out.reopenSettled = refused(() => setExchangeState(b, ExchangeState.LOADING));
out.flipSettled = refused(() => setExchangeState(b, ExchangeState.ERROR));
out.afterRefusals = b.dataset.state;
const c = fresh();
setExchangeState(c, ExchangeState.LOADING);
out.loadingTwice = refused(() => setExchangeState(c, ExchangeState.LOADING));
out.unknownState = refused(() => setExchangeState(c, 'stopped'));
console.log(JSON.stringify(out));
""" % json.dumps(_MODULE.as_uri())


def _run() -> dict:
    proc = subprocess.run(
        [_NODE, "--input-type=module", "-e", _HARNESS],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_RUN_TIMEOUT_S,
        creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_api_answers_derive_one_state_each():
    assert _run()["derived"] == ["ready", "empty", "error", "error"]


# state-machine-proof: board-exchange
def test_illegal_transition_refused_a_settled_pane_stays_settled():
    """A late response cannot reopen or flip a pane that already settled, and a pane
    cannot settle without first being loading or take a state outside the closed set."""
    got = _run()
    assert got["settleBeforeLoading"] is True
    assert got["afterEmpty"] == "empty"
    assert got["reopenSettled"] is True
    assert got["flipSettled"] is True
    assert got["afterRefusals"] == "empty"
    assert got["loadingTwice"] is True
    assert got["unknownState"] is True
