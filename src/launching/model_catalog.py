"""What a live probe showed about individual Copilot models."""

from __future__ import annotations

from typing import Any, Dict

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
    },
    # Took none, low, medium, high, xhigh and max; refused minimal.
    "gpt-6-luna": {
        "label": "GPT-6 Luna",
        "efforts": ("none", "low", "medium", "high", "xhigh", "max"),
    },
}


