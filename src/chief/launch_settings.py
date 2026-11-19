"""What the chief's saved launch settings mean for a start.

``chief_model`` is checked against the per-install ``copilot_models`` when it
is saved, but the default (or an edit to ``copilot_models`` since) can leave it
outside that list. Copilot would then be handed an id the launcher itself
refuses, so a start uses Copilot's own pick for it instead and says so.
"""

from __future__ import annotations

from typing import Tuple

from src.webapp_config import WebappConfig


def chief_model_problem(cfg: WebappConfig) -> str:
    """Why the saved chief model cannot be launched, ``""`` when it can."""
    model = cfg.chief_model
    if model in ("", "default", *cfg.copilot_models):
        return ""
    return (
        f"The chief's model {model!r} is not in the Copilot model list, so the chief "
        "used Copilot's own pick. Choose one of the listed models in Settings > Chief."
    )


def chief_launch_model(cfg: WebappConfig) -> Tuple[str, str]:
    """``(model, warning)`` for a chief start: the saved model, or ``"default"``
    (Copilot's own pick; ``""`` would mean the Coding agent's saved model to the
    flag builder) with the warning when it is not offered."""
    problem = chief_model_problem(cfg)
    return ("default", problem) if problem else (cfg.chief_model, "")
