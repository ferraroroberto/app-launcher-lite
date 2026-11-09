"""The scenario loop behind the ``run_flow`` fixture in ``conftest.py``.

One e2e test per user-visible flow (the fleet budget is under 15 e2e tests). A
flow test is a thin ``test_*`` that hands its scenarios to ``run_flow``; each
scenario is a plain function taking the fixtures it needs by name. Every
scenario gets its own browser context, page and (when it asks for one) PTY
session, exactly as a separate test did, so state and route mocks never leak
between scenarios. A failing scenario does not stop the rest: the flow fails
once, at the end, naming every scenario that broke with its traceback.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import time
import traceback
from typing import Callable, List

import pytest

logger = logging.getLogger(__name__)

# A flow runs many scenarios, so the 120 s per-test cap in pytest.ini (sized for
# one scenario) is raised for flow tests only.
FLOW_TIMEOUT_S = 600

# ``provide(name, stack, cache)`` returns the value of the fixture a scenario
# asked for; resources it opens are closed by ``stack`` when the scenario ends.
Provider = Callable[[str, contextlib.ExitStack, dict], object]


def scenario_label(scenario: Callable[..., object]) -> str:
    """The scenario's function name, with its bound keywords for a variant."""
    if not isinstance(scenario, functools.partial):
        return scenario.__name__
    extra = ",".join(f"{k}={v}" for k, v in scenario.keywords.items())
    return f"{scenario.func.__name__}[{extra}]"


def _fixture_names(scenario: Callable[..., object]) -> List[str]:
    # follow_wrapped=False: a decorator that sets a scenario up first declares
    # the fixtures it needs itself. Defaulted parameters are bound variants.
    sig = inspect.signature(scenario, follow_wrapped=False)
    return [
        name
        for name, p in sig.parameters.items()
        if p.default is p.empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]


def run_scenarios(scenarios: tuple, provide: Provider) -> None:
    """Run each scenario isolated; fail once at the end naming every failure."""
    failures: List[str] = []
    skips: List[str] = []
    passed = 0
    for scenario in scenarios:
        label = scenario_label(scenario)
        started = time.monotonic()
        try:
            with contextlib.ExitStack() as stack:
                cache: dict = {}
                scenario(**{
                    n: provide(n, stack, cache) for n in _fixture_names(scenario)
                })
        except pytest.skip.Exception as exc:
            skips.append(f"{label}: {exc}")
            logger.info("⏭️  scenario %s skipped: %s", label, exc)
            continue
        except (Exception, pytest.fail.Exception):
            failures.append(f"--- {label}\n{traceback.format_exc()}")
            logger.error("❌ scenario %s failed", label)
            continue
        passed += 1
        logger.info("✅ scenario %s (%.1fs)", label, time.monotonic() - started)
    if failures:
        pytest.fail(
            f"{len(failures)} of {len(scenarios)} scenario(s) failed "
            f"({passed} passed, {len(skips)} skipped):\n\n" + "\n".join(failures),
            pytrace=False,
        )
    if not passed:
        pytest.skip("every scenario skipped: " + "; ".join(skips))
