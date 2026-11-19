"""The chief's launch settings: model fallback and the context/effort flags."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.chief.launch_settings import chief_launch_model, chief_model_problem
from src.launching.flags import build_copilot_flags
from src.webapp_config import WebappConfig, _validate


def _cfg(**fields) -> WebappConfig:
    return replace(WebappConfig(), copilot_models=["gpt-5.6-terra"], **fields)


class TestChiefModel:

    @pytest.mark.parametrize("model", ["", "default", "gpt-5.6-terra"])
    def test_an_offered_or_default_model_is_launched_as_saved(self, model):
        cfg = _cfg(chief_model=model)
        assert chief_model_problem(cfg) == ""
        assert chief_launch_model(cfg) == (model, "")

    def test_a_model_outside_the_list_falls_back_with_a_fix_hint(self):
        cfg = _cfg(chief_model="gpt-6-luna")
        model, warning = chief_launch_model(cfg)
        assert model == "default"
        assert "gpt-6-luna" in warning and "Settings > Chief" in warning


class TestContextAndEffortFlags:

    def test_overrides_replace_the_saved_values(self):
        cfg = _cfg(copilot_context="long_context", copilot_effort="xhigh")
        flags = build_copilot_flags(
            cfg, model_override="gpt-5.6-terra",
            effort_override="low", context_override="default",
        )
        assert "--context default" in flags and "--effort low" in flags
        assert "long_context" not in flags and "xhigh" not in flags

    def test_no_override_keeps_the_saved_values(self):
        cfg = _cfg(copilot_context="long_context", copilot_effort="xhigh")
        flags = build_copilot_flags(cfg, model_override="gpt-5.6-terra", context_override="")
        assert "--context long_context" in flags and "--effort xhigh" in flags

    def test_effort_still_needs_an_explicit_model(self):
        cfg = _cfg()
        assert "--effort" not in build_copilot_flags(cfg, model_override="", effort_override="low")


class TestChiefFieldValidation:

    def test_unset_is_valid_and_is_the_default(self):
        cfg = _cfg()
        assert (cfg.chief_context, cfg.chief_effort) == ("", "")
        _validate(cfg)

    @pytest.mark.parametrize("context", ["default", "long_context"])
    @pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
    def test_every_value_copilot_accepts_is_valid(self, context, effort):
        _validate(_cfg(chief_context=context, chief_effort=effort))

    @pytest.mark.parametrize("fields", [{"chief_context": "huge"}, {"chief_effort": "extreme"}])
    def test_other_values_are_rejected(self, fields):
        with pytest.raises(ValueError):
            _validate(_cfg(**fields))
