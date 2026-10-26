"""SessionEventBus 领域端口与多订阅者读取语义测试。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import SessionContext
from agentsystem.outbound.event_bus import (
    EventReadBatch,
    SessionEventBus,
    StreamInspection,
)
from agentsystem.outbound.stream_models import (
    PublicEventCursor,
    PublicEventRecord,
    PublicStreamEvent,
    PublicStreamLifecycle,
    RunStartedPayload,
)

_EPOCH = UUID("5986c79d-15ce-4321-bd20-60838abc8438")
_NOW = datetime(2026, 8, 4, tzinfo=UTC)
_CTX = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def _cursor(sequence: int) -> PublicEventCursor:
    return PublicEventCursor(epoch=_EPOCH, stream_id=f"{sequence}-0")


def _record(sequence: int) -> PublicEventRecord:
    return PublicEventRecord(
        cursor=_cursor(sequence),
        event=PublicStreamEvent(
            event_id=f"event-{sequence}",
            type="run.started",
            created_at=_NOW,
            session_id="session",
            payload=RunStartedPayload(),
        ),
    )


class MemorySessionEventBus:
    """用于固定领域端口读取语义的无 Redis 合约 fake。"""

    def __init__(self) -> None:
        self.records: list[PublicEventRecord] = []
        self.lifecycle = PublicStreamLifecycle.OPEN

    async def append(
        self,
        ctx: SessionContext,
        event: PublicStreamEvent,
    ) -> PublicEventRecord:
        del ctx
        record = PublicEventRecord(
            cursor=_cursor(len(self.records) + 1),
            event=event,
        )
        self.records.append(record)
        return record

    async def inspect(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
    ) -> StreamInspection:
        del ctx
        self._validate(cursor)
        return StreamInspection(
            exists=bool(self.records),
            epoch=_EPOCH if self.records else None,
            lifecycle=self.lifecycle if self.records else None,
            first_cursor=self.records[0].cursor if self.records else None,
            last_cursor=self.records[-1].cursor if self.records else None,
        )

    async def read(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
        *,
        count: int,
        block_ms: int,
    ) -> EventReadBatch:
        del ctx, block_ms
        self._validate(cursor)
        sequence = int(cursor.stream_id.split("-", 1)[0]) if cursor else 0
        records = tuple(self.records[sequence : sequence + count])
        next_cursor = records[-1].cursor if records else cursor
        return EventReadBatch(
            records=records,
            next_cursor=next_cursor,
            lifecycle=self.lifecycle,
        )

    def _validate(self, cursor: PublicEventCursor | None) -> None:
        if cursor is None:
            return
        if cursor.epoch != _EPOCH:
            raise _cursor_error("cursor_expired", "cursor epoch 已失效")
        sequence = int(cursor.stream_id.split("-", 1)[0])
        if sequence < 1:
            raise _cursor_error("cursor_expired", "cursor 已早于首条保留事件")
        if sequence > len(self.records):
            raise _cursor_error("cursor_ahead", "cursor 超前")


@pytest.mark.asyncio
async def test_event_bus_protocol_supports_independent_replay_readers() -> None:
    """每个 reader 仅使用自己的 cursor，不竞争或消费共享 offset。"""
    memory_bus = MemorySessionEventBus()
    memory_bus.records.extend((_record(1), _record(2)))
    bus: SessionEventBus = memory_bus

    first_reader = await bus.read(_CTX, None, count=1, block_ms=0)
    second_reader = await bus.read(_CTX, None, count=2, block_ms=0)

    assert [record.cursor.stream_id for record in first_reader.records] == ["1-0"]
    assert [record.cursor.stream_id for record in second_reader.records] == [
        "1-0",
        "2-0",
    ]


@pytest.mark.asyncio
async def test_empty_read_preserves_cursor_and_terminal_lifecycle() -> None:
    """有限等待没有新事件时不得丢失当前位置或终态标识。"""
    bus = MemorySessionEventBus()
    bus.records.append(_record(1))
    bus.lifecycle = PublicStreamLifecycle.TERMINAL

    batch = await bus.read(_CTX, _cursor(1), count=10, block_ms=1)

    assert batch.records == ()
    assert batch.next_cursor == _cursor(1)
    assert batch.lifecycle is PublicStreamLifecycle.TERMINAL


@pytest.mark.asyncio
async def test_inspect_rejects_old_epoch_expired_and_ahead_cursors() -> None:
    """预检把 epoch、裁剪与超前位置区分为稳定异常。"""
    bus = MemorySessionEventBus()
    bus.records.extend((_record(1), _record(2)))

    with pytest.raises(ServiceError) as expired_epoch:
        await bus.inspect(
            _CTX,
            PublicEventCursor(epoch=UUID(int=1), stream_id="1-0"),
        )
    assert expired_epoch.value.code == "cursor_expired"
    with pytest.raises(ServiceError) as expired_position:
        await bus.inspect(_CTX, _cursor(0))
    assert expired_position.value.code == "cursor_expired"
    with pytest.raises(ServiceError) as ahead:
        await bus.inspect(_CTX, _cursor(3))
    assert ahead.value.code == "cursor_ahead"


def _cursor_error(code: str, message: str) -> ServiceError:
    return ServiceError(kind=ServiceErrorKind.CONFLICT, code=code, message=message)
