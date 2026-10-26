"""与具体 SDK 解耦的惰性流式 LLMProvider 协议。"""

from collections.abc import AsyncIterator
from typing import Protocol

from agentsystem.llm.models import LLMEvent, LLMRequest


class LLMProvider(Protocol):
    """产生规范化事件序列的异步模型提供方端口。

    将 SDK 类型和异常隔离在 adapter 内；调用方通过 LLMEnd 的完整响应确定终态，
    不以增量片段作为持久化内容权威。资源生命周期由具体实现和 Application 协作管理。
    """

    @property
    def provider_name(self) -> str:
        """返回用于 Trace 的稳定提供方名称。"""
        ...

    def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """返回惰性事件流；开始迭代时才执行外部 I/O。

        Args:
            request: 消息、工具声明、生成参数及事件关联信息。

        Returns:
            规范化事件迭代器，正常完成时以携带完整响应的 LLMEnd 结束。
        """
        ...
