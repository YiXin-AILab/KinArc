"""显式 null 移除请求输出上限，同时保留上下文安全校验。"""

from pathlib import Path
from typing import cast

import pytest
from agentsystem.config.models import CompactionConfig, ModelParamsConfig
from agentsystem.context.compactor import Compactor
from agentsystem.llm.client import LLMClient
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.models import LLMGenerationConfig
from benchmarks.evoagentbench.runners import official_baseline_worker as base
from pydantic import ValidationError
from tests.unit.llm.test_litellm_provider import request


def test_explicit_null_budget_survives_projection() -> None:
    params = ModelParamsConfig(model="openai/test", max_output_tokens=None)
    assert params.to_generation_config().max_output_tokens is None
    assert LLMGenerationConfig(model="test").max_output_tokens == 16384


async def test_uncapped_request_omits_both_output_limit_fields() -> None:
    query = request().model_copy(
        update={
            "config": LLMGenerationConfig(model="openai/test", max_output_tokens=None)
        }
    )
    arguments = await LiteLLMProvider()._request_arguments(query)
    assert "max_tokens" not in arguments
    assert "max_completion_tokens" not in arguments


def test_uncapped_compactor_retains_input_margin() -> None:
    compactor = Compactor(
        client=cast(LLMClient, None),
        generation_config=LLMGenerationConfig(model="test", max_output_tokens=None),
        context_window=128000,
        config=CompactionConfig(safety_margin_tokens=2048),
    )
    assert compactor._hard_input_limit == 125952


@pytest.mark.parametrize("value", [0, -1])
def test_nonpositive_budget_remains_invalid(value: int) -> None:
    with pytest.raises(ValidationError):
        ModelParamsConfig(model="test", max_output_tokens=value)


def test_base_null_removes_defaults_per_trial(tmp_path: Path) -> None:
    assert (
        base.subprocess_environment({"maxTokens": None}, tmp_path)[
            "EAB_UNCAPPED_OUTPUT"
        ]
        == "1"
    )
    assert "EAB_UNCAPPED_OUTPUT" not in base.subprocess_environment({}, tmp_path)
    original = {"model": "test", "max_tokens": 8192, "max_completion_tokens": 8192}
    assert base.output_budget_arguments(original, uncapped=True) == {"model": "test"}
    assert base.output_budget_arguments(original, uncapped=False) == original
    assert original["max_tokens"] == 8192
