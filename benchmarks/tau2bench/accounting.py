"""将 Runtime 用量映射到官方 Tau2 字段，不伪造零成本。"""

from typing import Any

from agentsystem.llm.models import Usage, UsageAccounting


def token_usage(usage: Usage) -> dict[str, Any]:
    """仅映射一次已观察到的 run 总用量，保留缓存和推理明细。"""
    result: dict[str, Any] = {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }
    if usage.cache_read_tokens is not None:
        result["prompt_tokens_details"] = {"cached_tokens": usage.cache_read_tokens}
    if usage.cache_write_tokens is not None:
        result["cache_creation_input_tokens"] = usage.cache_write_tokens
    if usage.reasoning_tokens is not None:
        result["completion_tokens_details"] = {
            "reasoning_tokens": usage.reasoning_tokens
        }
    return result


def estimate_cost(model: str, usage: Usage) -> float | None:
    """根据 LiteLLM 已知费率估算；用量或价格不完整时保持未知。"""
    import litellm

    if usage.accounting != UsageAccounting.EXACT:
        return None
    # 避免泛化模型名回退，否则可能产生虚假的零价格。
    prices = litellm.model_cost.get(model) or litellm.model_cost.get(
        model.removeprefix("openai/")
    )
    if prices is None or any(
        prices.get(key) is None
        for key in (
            "input_cost_per_token",
            "output_cost_per_token",
        )
    ):
        return None
    try:
        prompt, completion = litellm.cost_per_token(
            model=model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_tokens or 0,
            cache_creation_input_tokens=usage.cache_write_tokens or 0,
        )
    except (ValueError, KeyError):
        return None
    return float(prompt + completion)


def finalize_statistics(simulation: Any) -> None:
    """一方无法估价时，仍保留另一方已知的费用。"""
    for role in ("assistant", "user"):
        costs = [m.cost for m in simulation.messages if m.role == role]
        total = None if any(cost is None for cost in costs) else sum(costs)
        if role == "assistant":
            simulation.agent_cost = total
        else:
            simulation.user_cost = total
