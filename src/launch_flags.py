"""Copilot command-line composition — the launch shape of the coding agent.

Split off ``src/webapp_config.py`` (issue #691, a `/codebase-audit`
maintainability finding). That module owns *configuration*: the
:class:`~src.webapp_config.WebappConfig` dataclass plus its load / save /
validate plumbing. What lives here is not config plumbing at all — it is the
business logic that turns those persisted knobs into the exact argv the
agent's CLI expects.

* :func:`build_copilot_flags` — the normal launch line.
* :func:`build_resume_flags` — the same, with the agent's native resume token
  spliced ahead of the flags.

Imports one-way (``launch_flags`` → ``webapp_config``): the config module never
imports this one, so there is no cycle to work around.
"""

from __future__ import annotations

from typing import Optional

from src.agents import resume_command_for
from src.webapp_config import WebappConfig


def build_copilot_flags(
    cfg: WebappConfig, model_override: Optional[str] = None
) -> str:
    """Compose the `copilot` CLI flags from the persisted Copilot knobs.

    Semantics empirically verified on Copilot CLI 1.0.70:

    - ``--model`` is emitted only for a non-empty explicit id ("" and the
      "default" sentinel both mean "let Copilot pick auto" → omit). Explicit
      ids are tenant-gated — an unavailable id errors visibly in the PTY,
      which is acceptable.
    - ``--effort`` is emitted only when non-empty AND an explicit model is
      set: the auto model rejects it ("does not support reasoning effort
      configuration").
    - ``--context`` is emitted for "default"/"long_context" (omit on "").

    ``model_override`` forces a per-launch model — used by the Team OS
    launcher and Board — and follows the same rules: an empty/"default"
    override means no ``--model`` AND no ``--effort``.
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
    if cfg.copilot_context in ("default", "long_context"):
        parts.extend(["--context", cfg.copilot_context])
    if model and cfg.copilot_effort:
        parts.extend(["--effort", cfg.copilot_effort])
    return " ".join(parts)


def build_resume_flags(
    cfg: WebappConfig, agent_id: str, model_override: Optional[str] = None,
) -> str:
    """Compose the full flags string for a *Resume* launch (issue #151).

    Splices the agent's native resume token (see
    :func:`src.agents.resume_command_for`) ahead of the flags its resume
    path accepts, so the launch line becomes ``<command> <resume-token>
    <flags>`` and the agent renders its own session picker over the PTY.

    ``model_override`` forces a specific ``--model`` (the Team OS tab's
    per-launch combo rides through here on a Copilot resume). An agent with
    no resume path (e.g. ``ssh``) yields an empty token and just its normal
    flags — the caller treats an empty token as "not resumable" upstream.
    """
    token = resume_command_for(agent_id)
    if agent_id == "copilot":
        base = build_copilot_flags(cfg, model_override=model_override)
    else:
        base = ""
    return f"{token} {base}".strip()
