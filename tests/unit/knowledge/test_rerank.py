"""LiteLLM rerank 封装的解析、截断与失败收敛测试。"""

import asyncio
from typing import Any

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.knowledge.models import RerankConfig
from agentsystem.knowledge.rerank import LiteLLMReranker


def _config(**overrides: object) -> RerankConfig:
    payload: dict[str, object] = {"model": "jina_ai/jina-reranker-v2", **overrides}
    return RerankConfig.model_validate(payload)


async def test_rerank_sorts_by_score_and_truncates_top_n() -> None:
    """响应按 relevance_score 降序返回，并按 top_n 截断。"""

    async def fake_rerank(**kwargs: Any) -> dict[str, object]:
        assert kwargs["model"] == "jina_ai/jina-reranker-v2"
        assert kwargs["top_n"] == 1
        return {
            "results": [
                {"index": 1, "relevance_score": 0.2},
                {"index": 0, "relevance_score": 0.9},
            ]
        }

    reranker = LiteLLMReranker(_config(top_n=1), rerank=fake_rerank)

    results = await reranker.rerank("工时费", ["开户流程", "工时费标准"])

    assert [(item.index, item.score) for item in results] == [(0, 0.9)]


async def test_rerank_without_top_n_keeps_all_results() -> None:
    """未配置 top_n 时不向 Provider 传 top_n 并保留全部结果。"""

    async def fake_rerank(**kwargs: Any) -> dict[str, object]:
        assert "top_n" not in kwargs
        return {
            "results": [
                {"index": 0, "relevance_score": 0.4},
                {"index": 1, "relevance_score": 0.6},
            ]
        }

    reranker = LiteLLMReranker(_config(), rerank=fake_rerank)

    results = await reranker.rerank("工时费", ["开户流程", "工时费标准"])

    assert [(item.index, item.score) for item in results] == [(1, 0.6), (0, 0.4)]


async def test_rerank_invalid_response_raises_adapter_error() -> None:
    """下标越界或结构无效的响应收敛为 AdapterError。"""

    async def fake_rerank(**kwargs: Any) -> dict[str, object]:
        return {"results": [{"index": 9, "relevance_score": 0.9}]}

    reranker = LiteLLMReranker(_config(), rerank=fake_rerank)

    with pytest.raises(AdapterError, match="rerank"):
        await reranker.rerank("工时费", ["开户流程"])


async def test_rerank_timeout_is_retryable_adapter_error() -> None:
    """有限 timeout 触发后收敛为可重试 AdapterError。"""

    async def slow_rerank(**kwargs: Any) -> dict[str, object]:
        await asyncio.sleep(30)
        return {"results": []}

    reranker = LiteLLMReranker(
        _config(timeout_seconds=0.05),
        rerank=slow_rerank,
    )

    with pytest.raises(AdapterError) as captured:
        await reranker.rerank("工时费", ["开户流程"])

    assert captured.value.code == "knowledge_rerank_timeout"
    assert captured.value.retryable is True
