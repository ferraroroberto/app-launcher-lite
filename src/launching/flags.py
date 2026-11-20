"""Copilot command-line composition — the launch shape of the coding agent.

Split off ``src/webapp_config.py``. That module owns *configuration*: the
:class:`~src.webapp_config.WebappConfig` dataclass plus its load / save /
validate plumbing. What lives here is the business logic that turns those
persisted knobs into the exact argv the Copilot CLI expects.

* :func:`build_copilot_flags` — the normal launch line.
* :func:`build_resume_flags` — the same, with the agent's native resume token
  spliced ahead of it.

Imports one-way (``src.launching.flags`` → ``webapp_config``): the config module
never imports this one, so there is no cycle to work around.
"""

from __future__ import annotations

from typing import Optional

from src.agents import resume_command_for
from src.webapp_config import DEFAULT_COPILOT_MODELS, WebappConfig


def missing_default_copilot_models(cfg: WebappConfig) -> list:
    """Default model ids the saved ``copilot_models`` list does not carry.

    A saved list replaces the code default outright, so a model added to the
    default later never reaches an install that ever saved the list; Settings
    offers these ids so they are one tap away without editing JSON.
    """
    saved = set(cfg.copilot_models)
    return [m for m in DEFAULT_COPILOT_MODELS if m not in saved]


def unknown_model_detail(model: str) -> str:
    """The 400 detail for a model id outside ``copilot_models``, naming where to add it."""
    return (
        f"unknown model: {model} is not in the Copilot model list "
        "(Settings > Agents > GitHub Copilot > Models offered); add it there"
    )


def build_copilot_flags(
    cfg: WebappConfig,
    model_override: Optional[str] = None,
    effort_override: Optional[str] = None,
    context_override: Optional[str] = None,
) -> str:
    """Compose the `copilot` CLI flags from the persisted Copilot knobs.

    - ``--model`` is emitted only for a non-empty explicit id ("" and the
      "default" sentinel both mean "let Copilot pick auto" → omit). Explicit
      ids are gated by the account's plan: Copilot falls back to auto and
      prints one line when an id is not available, so ``copilot_models`` is
      a per-install list rather than a catalogue.
    - ``--effort`` is emitted only alongside an explicit model: the auto
      model rejects it ("does not support reasoning effort configuration").
    - ``--context`` is emitted for "default"/"long_context" (omit on "").
    - ``--autopilot`` and ``--allow-all`` follow their switches.

    ``model_override`` forces a per-launch model (Team OS, Board, chief) and
    follows the same rules: an empty/"default" override means no ``--model``
    and no ``--effort``. ``effort_override`` and ``context_override`` replace
    the saved effort and context for one launch (``None``/``""`` keep them).
    """
    parts: list[str] = []
    if cfg.copilot_skip_permissions:
        parts.append("--allow-all")
    raw = model_override if model_override is not None else cfg.copilot_model
    model = "" if raw in ("", "default") else raw
    if model:
        parts.extend(["--model", model])
    if cfg.copilot_autopilot:
        parts.append("--autopilot")
    context = context_override or cfg.copilot_context
    if context in ("default", "long_context"):
        parts.extend(["--context", context])
    effort = effort_override or cfg.copilot_effort
    if model and effort:
        parts.extend(["--effort", effort])
    return " ".join(parts)


def copilot_prompt_flag(prompt: str) -> str:
    """Return the ``-i "<prompt>"`` fragment that starts Copilot on ``prompt``.

    Copilot parses a bare positional as a subcommand and exits 1
    (``unrecognized subcommand``), so a prompted launch must go through
    ``-i``/``--interactive``, which keeps the session interactive and runs
    the prompt as the first turn. The prompt is wrapped in ``"`` because the
    caller splices it into the launch line; it must be a server-built
    command (a slug, an issue number, a launcher-owned path), never free text.
    """
    return f'-i "{prompt}"'


def build_resume_flags(
    cfg: WebappConfig, agent_id: str, model_override: Optional[str] = None,
    session_id: Optional[str] = None, *,
    effort_override: Optional[str] = None,
    context_override: Optional[str] = None,
) -> str:
    """Compose the full flags string for a *Resume* launch.

    Splices the agent's native resume token (see
    :func:`src.agents.resume_command_for`) ahead of the Copilot launch flags,
    so the launch line becomes ``<command> <resume-token> <flags>`` and Copilot
    renders its own session picker over the PTY.

    ``session_id``, when given, pins the resume token to that specific
    conversation (``<token> <session_id> <flags>``) so Copilot reattaches
    directly instead of rendering its picker. Omitted, the bare token renders
    the picker.

    ``model_override``, ``effort_override`` and ``context_override`` are one
    launch's values (a launch preset's), passed to :func:`build_copilot_flags`.
    """
    token = resume_command_for(agent_id)
    if session_id:
        token = f"{token} {session_id}"
    base = ""
    if agent_id == "copilot":
        base = build_copilot_flags(
            cfg, model_override=model_override, effort_override=effort_override,
            context_override=context_override,
        )
    return f"{token} {base}".strip()
