"""传输无关的公开事件订阅边界。"""

from collections.abc import Awaitable, Callable

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import SessionContext
from agentsystem.outbound.event_bus import (
    EventReadBatch,
    SessionEventBus,
)
from agentsystem.outbound.stream_models import (
    PublicEventCursor,
    PublicEventRecord,
    PublicStreamEvent,
)


class SessionEventSubscription:
    """管理单个连接的公开事件读取游标，不持有共享 Stream 的所有权。

    每次读取使用有限阻塞，游标仅在本实例推进。边界事件经回调确认后提供
    should_close 提示；调用方仍负责关闭传输连接和本订阅。
    """

    def __init__(
        self,
        *,
        ctx: SessionContext,
        event_bus: SessionEventBus,
        cursor: PublicEventCursor | None,
        read_batch_size: int,
        block_ms: int,
        can_close_at: Callable[[SessionContext, PublicStreamEvent], Awaitable[bool]],
    ) -> None:
        """保存连接私有位置，不向共享存储写 subscriber offset。

        Args:
            ctx: 被订阅的会话定位。
            event_bus: 共享公开事件读取端口。
            cursor: 已读位置，None 表示从端口定义的起点读取。
            read_batch_size: 单批最多读取的记录数。
            block_ms: 单次读取的阻塞毫秒数。
            can_close_at: 确认边界事件仍对应当前空闲会话的异步回调。
        """
        self._ctx = ctx
        self._event_bus = event_bus
        self._cursor = cursor
        self._read_batch_size = read_batch_size
        self._block_ms = block_ms
        self._can_close_at = can_close_at
        self._closed = False
        self._should_close = False

    @property
    def should_close(self) -> bool:
        """返回当前 batch 后是否已到达 session 空闲边界。"""
        return self._should_close

    async def read_next(self) -> EventReadBatch:
        """读取下一批事件，推进本地游标并重新判断是否可以关闭。

        Returns:
            端口返回的事件批次，可能因阻塞到期而为空。

        Raises:
            RuntimeError: 本订阅已经关闭。

        事件读取和关闭判断回调的异常直接传播；回调执行前游标已推进。
        """
        if self._closed:
            raise RuntimeError("SessionEventSubscription 已关闭")
        batch = await self._event_bus.read(
            self._ctx,
            self._cursor,
            count=self._read_batch_size,
            block_ms=self._block_ms,
        )
        self._cursor = batch.next_cursor
        boundary_types = {
            "run.suspended",
            "run.completed",
            "run.failed",
            "run.canceled",
        }
        boundary = next(
            (
                record.event
                for record in reversed(batch.records)
                if record.event.type in boundary_types
            ),
            None,
        )
        self._should_close = boundary is not None and await self._can_close_at(
            self._ctx, boundary
        )
        return batch

    async def close(self) -> None:
        """幂等关闭订阅，不删除共享 Stream。"""
        self._closed = True


def parse_cursor(cursor: str | None) -> PublicEventCursor | None:
    """把外部 cursor 解析为领域值，并统一非法输入异常。

    Args:
        cursor: 外部游标文本；None 表示未指定。

    Returns:
        解析后的游标，或保持 None。

    Raises:
        ServiceError: 文本格式非法，错误码为 invalid_cursor。
    """
    if cursor is None:
        return None
    try:
        return PublicEventCursor.parse(cursor)
    except (TypeError, ValueError) as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_cursor",
            message="cursor 格式非法",
        ) from error


__all__ = ["PublicEventRecord", "SessionEventSubscription"]
