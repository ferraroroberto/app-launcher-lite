"""Canonical coding-harness model catalog.

Model ids are deliberately explicit and subscription-path aware.  Refresh the
catalog with the commands documented in README.md; unavailable rollout models
remain visible to the UI but are never accepted by a launch builder.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

# ``default_effort`` is the model's own documented default (Anthropic's effort
# page: "Claude Opus 5.5 and Claude Haiku 5.5 default to medium", every other
# model to high), so sending it is the same as sending nothing. The ``opus``
# alias is Opus 5.5 today; ``tests/unit/launching/test_effort_resolver.py`` pins the value, so
# an alias that moves to another Opus forces a deliberate edit here.
CLAUDE_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "sonnet": {
        "label": "Sonnet", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "high", "available": True,
    },
    "opus": {
        "label": "Opus", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "medium", "available": True,
    },
    "fable": {
        "label": "Fable", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "high", "available": True,
    },
}

# The legacy Haiku's display name. It is not in ``CLAUDE_MODEL_SPECS`` (the
# Settings dropdowns never offer it) but a resume and an in-session switch
# both can run on it, so those two read ``CLAUDE_SESSION_MODEL_SPECS``.
LEGACY_CLAUDE_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "haiku": {"label": "Haiku", "available": True},
}
CLAUDE_SESSION_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    **LEGACY_CLAUDE_MODEL_SPECS, **CLAUDE_MODEL_SPECS,
}

# Ids from ``codex debug models`` (CLI 0.164.0). Luna 6.0 replaced Luna 5.6;
# ``RENAMED_CODEX_MODELS`` below carries a saved id over.
CODEX_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "gpt-6-luna": {
        "label": "Luna", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "xhigh", "available": True,
    },
    "gpt-5.6-terra": {
        "label": "Terra", "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
        "default_effort": "medium", "available": True,
    },
    "gpt-5.6-sol": {
        "label": "Sol", "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
        "default_effort": "low", "available": True,
    },
    "gpt-6-astra": {
        "label": "Astra", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "medium", "available": True,
    },
}

# Pi is intentionally restricted to the two subscription-backed providers.
# ``default_effort`` is ``high`` throughout: nothing is published for
# Pi's providers, and Pi pins Opus 5 (not 5.5), whose documented default is high.
# Pi's ``--thinking`` ladder is ``VALID_PI_EFFORTS``, so there is no per-spec
# ``efforts`` list.
# Never add the native ``anthropic`` provider here: it uses metered API credit.
PI_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "claude-opus-5": {
        "label": "Opus", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-opus-5",
        "default_effort": "high", "available": True,
    },
    "claude-sonnet-5": {
        "label": "Sonnet", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-sonnet-5",
        "default_effort": "high", "available": True,
    },
    "claude-fable-5": {
        "label": "Fable", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-fable-5",
        "default_effort": "high", "available": True,
    },
    "gpt-5.6-luna": {
        "label": "Luna", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-luna",
        "default_effort": "high", "available": True,
    },
    "gpt-5.6-terra": {
        "label": "Terra", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-terra",
        "default_effort": "high", "available": True,
    },
    "gpt-5.6-sol": {
        "label": "Sol", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-sol",
        "default_effort": "high", "available": True,
    },
    "gpt-6-astra": {
        "label": "Astra", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-6-astra",
        "default_effort": "high", "available": False,
        "unavailable_reason": "Not advertised by the installed Pi Codex provider yet",
    },
}

# GitHub Copilot. Which model ids a launch may use is the account's call, so the
# offered list is the per-install ``copilot_models`` setting, not this table. A
# row here records only what a live probe of the CLI showed about one model: the
# efforts it took (``copilot --model <id> --effort <level>``, CLI 1.0.114, probed
# 2026-11-20: each level returned a reply, the refused ones errored with
# ``Reasoning effort "<level>" is not supported for model "<id>"``).
# A model with no row is claimed to accept nothing in particular, so a preset on
# it may carry any level the CLI itself takes (``VALID_COPILOT_EFFORTS``).
COPILOT_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    # Took low, medium, high, xhigh and max; refused none and minimal.
    "mai-code-2-flash": {
        "label": "MAI Code 2 Flash",
        "efforts": ("low", "medium", "high", "xhigh", "max"),
        "available": True,
    },
    # Took none, low, medium, high, xhigh and max; refused minimal.
    "gpt-6-luna": {
        "label": "GPT-6 Luna",
        "efforts": ("none", "low", "medium", "high", "xhigh", "max"),
        "available": True,
    },
}


def available_values(specs: Dict[str, Dict[str, Any]]) -> Tuple[str, ...]:
    """Return launchable ids in authored display order."""
    return tuple(value for value, spec in specs.items() if spec.get("available"))
