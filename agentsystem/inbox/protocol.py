"""与具体消息中间件无关的异步 Inbox 协议。"""

from collections.abc import Sequence
from typing import Protocol

from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    ReadyEntry,
    ResumeReady,
    ResumeRequest,
)


class Inbox(Protocol):
    """普通输入与 Ready 控制通道的异步领域端口。

    普通消息先领取、保存后完成；恢复请求只进入控制通道。协议不负责分配
    run_id 或执行 Agent，调度和消息保存屏障由调用方协调。
    """

    async def push(
        self,
        ctx: SessionContext,
        message: InboundMessage,
    ) -> None:
        """将普通输入按消息标识幂等写入 Inbox，再追加 NormalReady。

        Args:
            ctx: 目标会话的完整作用域。
            message: 普通输入，session_id 须与 ctx 匹配。

        重复的相同消息仍可产生 Ready；输入写入与 Ready 追加不构成一个事务。
        """
        ...

    async def push_resume(self, request: ResumeRequest) -> None:
        """追加恢复控制消息，不写普通 Inbox。

        Args:
            request: 包含已有 run 身份、单次 token 和恢复输入的请求。
        """
        ...

    async def read_ready(
        self,
        consumer_id: str,
        *,
        agent_ids: Sequence[str],
        count: int,
        block_ms: int,
    ) -> tuple[ReadyEntry, ...]:
        """领取支持的 Agent 的新 Ready，不建立待确认记录。

        Args:
            consumer_id: 当前消费方标识。
            agent_ids: 当前节点支持的 Agent 集合。
            count: 传给底层读取操作的批量参数。
            block_ms: 底层读取阻塞毫秒数；Redis 中零表示持续阻塞，
                外层调用期限仍由连接配置限制。

        Returns:
            本次成功解码的 Ready 元组；没有可用消息时为空。
            Redis 实现会记录并跳过坏消息，不进行重放。
        """
        ...

    async def requeue_resume(self, ready: ResumeReady) -> None:
        """保持同一恢复 payload 重新追加控制消息。

        Args:
            ready: 锁忙时需要重投的 ResumeReady，保留 resume_id 和 token。
        """
        ...

    async def claim(
        self,
        ctx: SessionContext,
        modes: frozenset[InboundMode],
        *,
        owner_token: str,
    ) -> tuple[InboundMessage, ...]:
        """按入队顺序领取匹配输入，保留正文直到保存屏障后完成。

        Args:
            ctx: 目标会话的完整作用域。
            modes: 本次允许领取的模式；空集合返回空元组。
            owner_token: 非空领取者令牌，用于完成确认。

        Returns:
            本次成功领取的输入，按原始入队顺序排列。
        """
        ...

    async def complete(
        self,
        ctx: SessionContext,
        message_ids: tuple[str, ...],
        *,
        owner_token: str,
    ) -> None:
        """由领取 owner 确认已保存消息并删除队列数据。

        Args:
            ctx: 目标会话的完整作用域。
            message_ids: 已越过保存屏障的消息标识；空元组无操作。
            owner_token: 与领取记录匹配的非空令牌。

        调用方负责先完成消息持久化；该操作不负责写入 MessageManager。
        """
        ...

    async def list_pending(
        self,
        ctx: SessionContext,
    ) -> tuple[InboundMessage, ...]:
        """按原始入队顺序读取所有尚未完成的普通消息。

        Args:
            ctx: 目标会话的完整作用域。

        Returns:
            包含已领取和未领取输入的快照，不改变领取状态。
        """
        ...
