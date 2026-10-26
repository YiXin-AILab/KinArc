"""与 Redis/ASGI 解耦的 session 公开事件总线领域端口。"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agentsystem.common.models import SessionContext
from agentsystem.outbound.stream_models import (
    PublicEventCursor,
    PublicEventRecord,
    PublicStreamEvent,
    PublicStreamLifecycle,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamInspection:
    """连接建立前对 Stream 存在性、边界和生命周期的快照。

    不代表整个读取过程的事务快照；不存在的流必须省略所有元数据。

    Attributes:
        exists: 是否存在可读取的流。
        epoch: 当前流世代，防止过期游标误用于重建后的流。
        lifecycle: 当前写入生命周期。
        first_cursor: 首条保留事件的位置。
        last_cursor: 最后一条事件的位置。
    """

    exists: bool
    epoch: UUID | None = None
    lifecycle: PublicStreamLifecycle | None = None
    first_cursor: PublicEventCursor | None = None
    last_cursor: PublicEventCursor | None = None

    def __post_init__(self) -> None:
        """存在标志必须与 metadata 和边界同时出现或同时缺失。"""
        metadata = (
            self.epoch,
            self.lifecycle,
            self.first_cursor,
            self.last_cursor,
        )
        if self.exists and any(item is None for item in metadata):
            raise ValueError("存在的 Stream 必须包含 epoch、lifecycle 和完整边界")
        if not self.exists and any(item is not None for item in metadata):
            raise ValueError("不存在的 Stream 不得包含 metadata 或边界")


@dataclass(frozen=True, slots=True, kw_only=True)
class EventReadBatch:
    """一次有限 EventBus 读取及连接应推进到的位置。

    Attributes:
        records: 按总线顺序排列的事件，可为空。
        next_cursor: 非空时为末条记录游标，空读取可保留原游标。
        lifecycle: 读取时获知的流生命周期，未知时为 None。
    """

    records: tuple[PublicEventRecord, ...] = ()
    next_cursor: PublicEventCursor | None = None
    lifecycle: PublicStreamLifecycle | None = None

    def __post_init__(self) -> None:
        """非空 batch 必须推进到最后一条 record 的 cursor。"""
        if self.records and self.next_cursor != self.records[-1].cursor:
            raise ValueError("next_cursor 必须等于最后一条 record cursor")


class SessionEventBus(Protocol):
    """按 session 隔离、支持多个独立 cursor reader 的公开事件端口。

    调用方维护各自游标，总线不保存订阅者 offset；公开流仅用于交付和恢复，
    不替代 MessageManager、StateManager 的权威快照。
    """

    async def append(
        self,
        ctx: SessionContext,
        event: PublicStreamEvent,
    ) -> PublicEventRecord:
        """原子追加事件，并返回权威有序 cursor。

        Args:
            ctx: 目标 session 的隔离上下文。
            event: 已完成公开字段映射的事件。

        Returns:
            带总线分配游标的记录。
        """
        ...

    async def inspect(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
    ) -> StreamInspection:
        """预检 Stream 与 cursor；无效 cursor 抛出领域异常。

        Args:
            ctx: 目标 session 的隔离上下文。
            cursor: 待恢复位置；None 表示新订阅者。

        Returns:
            流是否存在以及保留边界和生命周期。
        """
        ...

    async def read(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
        *,
        count: int,
        block_ms: int,
    ) -> EventReadBatch:
        """从 cursor 之后有限读取；无 cursor 时从当前保留开头读取。

        Args:
            ctx: 目标 session 的隔离上下文。
            cursor: 已消费的末条位置，可为 None。
            count: 最多读取的记录数，必须为正数。
            block_ms: 最长阻塞毫秒数，零表示立即返回。

        Returns:
            事件批次及下一次读取应使用的游标。
        """
        ...
