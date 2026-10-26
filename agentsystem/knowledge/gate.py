"""基于小模型的逐条相关性裁判，失败时降级为 partial 而不炸主流程。"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import litellm

from agentsystem.common.errors import AdapterError
from agentsystem.knowledge.documents import KnowledgeDocument
from agentsystem.knowledge.models import RelevanceGateConfig, RelevanceLevel

LOGGER = logging.getLogger(__name__)

type CompletionCallable = Callable[..., Awaitable[Any]]
"""与 litellm.acompletion 调用形态兼容的可注入异步 callable。"""

_JSON_BLOCK_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
_MAX_ATTEMPTS = 2
_CONTENT_PREVIEW_CHARS = 2_000

_SYSTEM_PROMPT = """\
你是检索结果相关性裁判。给定用户问题和一条知识库文档，判断该文档与问题的相关性。
只输出一个 JSON 对象，不要输出任何其他内容：
{"relevance": "relevant" | "partial" | "irrelevant", "reason": "一句话理由"}
- relevant：文档直接回答了问题或包含问题所需的明确信息。
- partial：文档与问题相关但只覆盖部分信息，或需要结合其他信息才能回答。
- irrelevant：文档与问题无关。"""


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """一条候选文档的相关性判定；gate_error 标记降级结果。"""

    doc_id: str
    relevance: RelevanceLevel
    reason: str
    gate_error: bool = False


class RelevanceGate:
    """对最终候选并行调用小模型裁判，整体耗时受单条 timeout 约束。"""

    def __init__(
        self,
        config: RelevanceGateConfig,
        *,
        completion: CompletionCallable | None = None,
    ) -> None:
        """保存调用配置，并允许测试注入确定的 completion callable。

        Args:
            config: 相关性裁判模型的连接与超时配置。
            completion: 可注入的异步模型调用，省略时使用 LiteLLM。
        """
        self._config = config
        self._completion = completion or cast(CompletionCallable, litellm.acompletion)

    async def judge(
        self,
        query: str,
        documents: Sequence[KnowledgeDocument],
    ) -> list[GateVerdict]:
        """并行判断候选相关性，单条失败重试一次后降级为 PARTIAL。

        Args:
            query: 用户查询文本。
            documents: 待判断的候选文档。

        Returns:
            与输入顺序一致的判定列表；降级条目的 gate_error 为 True。
        """
        return list(
            await asyncio.gather(
                *(self._judge_one(query, document) for document in documents)
            )
        )

    async def _judge_one(
        self,
        query: str,
        document: KnowledgeDocument,
    ) -> GateVerdict:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                text = await self._complete(query, document)
                relevance, reason = _parse_verdict(text)
                return GateVerdict(
                    doc_id=document.doc_id,
                    relevance=relevance,
                    reason=reason,
                )
            except (AdapterError, ValueError) as error:
                LOGGER.warning(
                    "knowledge_gate_failed doc_id=%s attempt=%d error_type=%s",
                    document.doc_id,
                    attempt,
                    type(error).__name__,
                )
        return GateVerdict(
            doc_id=document.doc_id,
            relevance=RelevanceLevel.PARTIAL,
            reason="",
            gate_error=True,
        )

    async def _complete(self, query: str, document: KnowledgeDocument) -> str:
        config = self._config
        arguments: dict[str, Any] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"问题：{query}\n\n"
                        f"文档标题：{document.title}\n"
                        f"文档内容：{document.content[:_CONTENT_PREVIEW_CHARS]}"
                    ),
                },
            ],
            "timeout": config.timeout_seconds,
        }
        if config.api_key is not None:
            arguments["api_key"] = config.api_key.get_secret_value()
        if config.base_url is not None:
            arguments["base_url"] = config.base_url
        try:
            response = await asyncio.wait_for(
                self._completion(**arguments),
                timeout=config.timeout_seconds,
            )
        except AdapterError:
            raise
        except TimeoutError as error:
            raise AdapterError(
                operation="knowledge.gate",
                code="knowledge_gate_timeout",
                message="知识库相关性裁判调用超时",
                retryable=True,
            ) from error
        except Exception as error:
            raise AdapterError(
                operation="knowledge.gate",
                code="knowledge_gate_failed",
                message="知识库相关性裁判调用失败",
            ) from error
        content = _extract_content(response)
        if content is None:
            raise AdapterError(
                operation="knowledge.gate",
                code="knowledge_gate_invalid_response",
                message="知识库相关性裁判响应无效",
            )
        return content


def _parse_verdict(text: str) -> tuple[RelevanceLevel, str]:
    """从模型输出中提取 JSON 判定；无法解析时抛出 ValueError 触发重试。"""
    match = _JSON_BLOCK_PATTERN.search(text)
    if match is None:
        raise ValueError("裁判输出缺少 JSON 对象")
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise ValueError("裁判输出不是合法 JSON") from error
    if not isinstance(raw, dict):
        raise ValueError("裁判输出必须是 JSON 对象")
    try:
        relevance = RelevanceLevel(str(raw.get("relevance")))
    except ValueError as error:
        raise ValueError("裁判输出 relevance 取值无效") from error
    reason = raw.get("reason")
    return relevance, reason if isinstance(reason, str) else ""


def _extract_content(response: Any) -> str | None:
    choices = _value(response, "choices", None)
    if not isinstance(choices, Sequence) or isinstance(choices, str) or not choices:
        return None
    message = _value(choices[0], "message", None)
    content = _value(message, "content", None)
    return content if isinstance(content, str) else None


def _value(source: Any, name: str, default: Any) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)
