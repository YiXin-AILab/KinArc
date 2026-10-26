"""LiteLLM rerank 调用的模块内封装，不泄漏任何 LiteLLM 类型。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import litellm

from agentsystem.common.errors import AdapterError
from agentsystem.knowledge.models import RerankConfig

type RerankCallable = Callable[..., Awaitable[Any]]
"""与 litellm.arerank 调用形态兼容的可注入异步 callable。"""


@dataclass(frozen=True, slots=True)
class RerankResult:
    """一条重排序命中的原文档下标与相关性得分。"""

    index: int
    score: float


class LiteLLMReranker:
    """把 LiteLLM arerank 收敛为纯 Python 的 (下标, 得分) 列表。"""

    def __init__(
        self,
        config: RerankConfig,
        *,
        rerank: RerankCallable | None = None,
    ) -> None:
        """保存调用配置，并允许测试注入确定的 rerank callable。

        Args:
            config: 重排序模型的连接、超时及返回数量配置。
            rerank: 可注入的异步重排序调用，省略时使用 LiteLLM。
        """
        self._config = config
        self._rerank = rerank or cast(RerankCallable, litellm.arerank)

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
    ) -> list[RerankResult]:
        """对候选文档重排序，按相关性得分降序返回，截断到 top_n。

        Args:
            query: 用于判断相关性的查询文本。
            documents: 候选文档正文，返回结果的 index 对应此序列下标。

        Returns:
            重排序后的文档下标与得分；候选为空时返回空列表。

        Raises:
            AdapterError: 调用超时、Provider 失败或响应结构无效。
        """
        if not documents:
            return []
        config = self._config
        arguments: dict[str, Any] = {
            "model": config.model,
            "query": query,
            "documents": list(documents),
            "timeout": config.timeout_seconds,
        }
        if config.top_n is not None:
            arguments["top_n"] = config.top_n
        if config.api_key is not None:
            arguments["api_key"] = config.api_key.get_secret_value()
        if config.base_url is not None:
            arguments["api_base"] = config.base_url
        try:
            response = await asyncio.wait_for(
                self._rerank(**arguments),
                timeout=config.timeout_seconds,
            )
        except AdapterError:
            raise
        except TimeoutError as error:
            raise AdapterError(
                operation="knowledge.rerank",
                code="knowledge_rerank_timeout",
                message="知识库 rerank 调用超时",
                retryable=True,
            ) from error
        except Exception as error:
            raise AdapterError(
                operation="knowledge.rerank",
                code="knowledge_rerank_failed",
                message="知识库 rerank 调用失败",
            ) from error
        results = _parse_results(response, document_count=len(documents))
        if config.top_n is not None:
            results = results[: config.top_n]
        return results


def _parse_results(response: Any, *, document_count: int) -> list[RerankResult]:
    raw_results = _value(response, "results", None)
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, str):
        raise AdapterError(
            operation="knowledge.rerank",
            code="knowledge_rerank_invalid_response",
            message="知识库 rerank 响应无效",
        )
    results: list[RerankResult] = []
    for item in raw_results:
        index = _value(item, "index", None)
        score = _value(item, "relevance_score", None)
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < document_count
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
        ):
            raise AdapterError(
                operation="knowledge.rerank",
                code="knowledge_rerank_invalid_response",
                message="知识库 rerank 响应无效",
            )
        results.append(RerankResult(index=index, score=float(score)))
    return sorted(results, key=lambda item: item.score, reverse=True)


def _value(source: Any, name: str, default: Any) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)
