"""LiteLLM embedding 调用的模块内封装，不泄漏任何 LiteLLM 类型。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

import litellm

from agentsystem.common.errors import AdapterError
from agentsystem.knowledge.models import EmbeddingConfig

type EmbeddingCallable = Callable[..., Awaitable[Any]]
"""与 litellm.aembedding 调用形态兼容的可注入异步 callable。"""

type EmbedTexts = Callable[[Sequence[str]], Awaitable[list[list[float]]]]
"""知识库内部统一的文本向量化端口。"""


class LiteLLMEmbedder:
    """把 LiteLLM aembedding 收敛为纯 Python 向量列表。"""

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        embedding: EmbeddingCallable | None = None,
    ) -> None:
        """保存调用配置，并允许测试注入确定的 embedding callable。

        Args:
            config: 向量模型的连接与超时配置。
            embedding: 可注入的异步向量化调用，省略时使用 LiteLLM。
        """
        self._config = config
        self._embedding = embedding or cast(EmbeddingCallable, litellm.aembedding)

    @property
    def model(self) -> str:
        """返回索引持久化用于失效判断的 embedding 模型名。"""
        return self._config.model

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """批量计算文本向量，将外部调用失败收敛为 AdapterError。

        Args:
            texts: 待向量化的文本序列。

        Returns:
            与输入顺序一致的向量列表；输入为空时返回空列表。

        Raises:
            AdapterError: 调用超时、Provider 失败或响应结构无效。
        """
        if not texts:
            return []
        config = self._config
        arguments: dict[str, Any] = {
            "model": config.model,
            "input": list(texts),
            "timeout": config.timeout_seconds,
        }
        if config.api_key is not None:
            arguments["api_key"] = config.api_key.get_secret_value()
        if config.base_url is not None:
            arguments["api_base"] = config.base_url
        try:
            response = await asyncio.wait_for(
                self._embedding(**arguments),
                timeout=config.timeout_seconds,
            )
        except AdapterError:
            raise
        except TimeoutError as error:
            raise AdapterError(
                operation="knowledge.embed",
                code="knowledge_embedding_timeout",
                message="知识库 embedding 调用超时",
                retryable=True,
            ) from error
        except Exception as error:
            raise AdapterError(
                operation="knowledge.embed",
                code="knowledge_embedding_failed",
                message="知识库 embedding 调用失败",
            ) from error
        return _parse_vectors(response, expected=len(texts))


def _parse_vectors(response: Any, *, expected: int) -> list[list[float]]:
    data = _value(response, "data", None)
    if not isinstance(data, Sequence) or isinstance(data, str):
        raise AdapterError(
            operation="knowledge.embed",
            code="knowledge_embedding_invalid_response",
            message="知识库 embedding 响应无效",
        )
    vectors: list[list[float]] = []
    for item in data:
        vector = _value(item, "embedding", None)
        if not isinstance(vector, Sequence) or isinstance(vector, str):
            raise AdapterError(
                operation="knowledge.embed",
                code="knowledge_embedding_invalid_response",
                message="知识库 embedding 响应无效",
            )
        vectors.append([float(component) for component in vector])
    if len(vectors) != expected or len({len(vector) for vector in vectors}) > 1:
        raise AdapterError(
            operation="knowledge.embed",
            code="knowledge_embedding_invalid_response",
            message="知识库 embedding 响应向量数量或维度不一致",
        )
    return vectors


def _value(source: Any, name: str, default: Any) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)
