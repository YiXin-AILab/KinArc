"""Official statistics retain usage and distinguish unknown from zero cost."""

from types import SimpleNamespace

import pytest
from agentsystem.llm.models import Usage
from benchmarks.tau2bench.accounting import (
    estimate_cost,
    finalize_statistics,
    token_usage,
)


def test_usage_preserves_cache_and_reasoning_tokens() -> None:
    usage = Usage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        accounting="exact",
        cache_read_tokens=4,
        reasoning_tokens=2,
    )
    result = token_usage(usage)
    assert result["prompt_tokens"] == 10
    assert result["completion_tokens"] == 5
    assert result["prompt_tokens_details"] == {"cached_tokens": 4}
    assert result["completion_tokens_details"] == {"reasoning_tokens": 2}


def test_price_known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    monkeypatch.setitem(
        litellm.model_cost,
        "review-priced",
        {
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.02,
            "litellm_provider": "openai",
            "mode": "chat",
        },
    )
    usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15, accounting="exact")
    assert estimate_cost("openai/review-priced", usage) == pytest.approx(0.2)


def test_unknown_price_and_partial_usage_are_not_free() -> None:
    usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15, accounting="exact")
    assert estimate_cost("openai/unpriced-review-model", usage) is None
    assert estimate_cost("gpt-4o", Usage(total_tokens=15)) is None


def test_missing_agent_cost_does_not_erase_user_cost() -> None:
    simulation = SimpleNamespace(
        messages=[
            SimpleNamespace(role="assistant", cost=None),
            SimpleNamespace(role="user", cost=0.25),
            SimpleNamespace(role="tool", cost=None),
        ],
        agent_cost=None,
        user_cost=None,
    )
    finalize_statistics(simulation)
    assert simulation.agent_cost is None
    assert simulation.user_cost == 0.25
