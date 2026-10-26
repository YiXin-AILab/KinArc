"""基于 Redis Stream 的可恢复、多订阅者 Session EventBus。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.config.models import PublicStreamConfig
from agentsystem.outbound.event_bus import (
    EventReadBatch,
    StreamInspection,
)
from agentsystem.outbound.redis_event_keys import PublicEventRedisKeys
from agentsystem.outbound.stream_models import (
    PublicEventCursor,
    PublicEventRecord,
    PublicStreamEvent,
    PublicStreamLifecycle,
)
from agentsystem.redis.client import RedisClient, await_redis
from agentsystem.redis.codec import RedisJsonCodec

_SCRIPT = (Path(__file__).with_name("scripts") / "append_event.lua").read_text(
    encoding="utf-8"
)
_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
type DiagnosticSink = Callable[["EventBusDiagnostic"], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class EventBusDiagnostic:
    """不包含 session 身份或事件 payload 的安全操作诊断。

    Attributes:
        operation: append 或 read 操作名称。
        duration_ms: 使用单调时钟测得的毫秒耗时。
        event_type: 追加事件的类型，读取时可为空。
        stream_length: 追加后的流长度或本次读取记录数。
        outcome: ok 或空读取等操作结果标签。
    """

    operation: str
    duration_ms: float
    event_type: str | None = None
    stream_length: int | None = None
    outcome: str = "ok"


class RedisSessionEventBus:
    """每个 session 一个独立 epoch Stream 的异步 EventBus adapter。

    注入读写 client 并由 Application 管理连接；Lua 负责追加、裁剪和 TTL 的原子性，
    reader 各自提交游标，不在 Redis 保存消费 offset。预检与读取并非原子快照。
    """

    def __init__(
        self,
        write_client: RedisClient,
        read_client: RedisClient,
        *,
        prefix: str,
        config: PublicStreamConfig,
        clock: Callable[[], datetime] | None = None,
        epoch_factory: Callable[[], UUID] | None = None,
        diagnostic_sink: DiagnosticSink | None = None,
    ) -> None:
        """接收 Application 管理的 clients；adapter 不拥有连接生命周期。

        Args:
            write_client: 执行原子追加脚本的 client。
            read_client: 查询边界并执行有限阻塞读取的 client。
            prefix: 公开事件 key 的应用命名空间。
            config: 留存、大小及终态宽限配置。
            clock: TTL 计算的墙上时钟，缺省为当前 UTC 时间。
            epoch_factory: 新流世代 UUID 工厂，缺省为 uuid4。
            diagnostic_sink: 可选同步诊断回调，其异常不会被吞掉。
        """
        self._write_client = write_client
        self._read_client = read_client
        self._keys = PublicEventRedisKeys(prefix)
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._epoch_factory = epoch_factory or uuid4
        self._diagnostic_sink = diagnostic_sink
        self._codec = RedisJsonCodec()

    async def append(
        self,
        ctx: SessionContext,
        event: PublicStreamEvent,
    ) -> PublicEventRecord:
        """通过 Lua 原子创建 epoch、追加、裁剪并刷新双 key TTL。

        Args:
            ctx: 目标 session 的隔离上下文。
            event: 已映射好的公开事件。

        Returns:
            包含 Redis 分配位置与当前 epoch 的记录。

        Raises:
            ValueError: session 不匹配或编码后的事件超限。
        """
        if event.session_id != ctx.session_id:
            raise ValueError("PublicStreamEvent.session_id 与 SessionContext 不一致")
        encoded = self._encode(event)
        if len(encoded) > self._config.max_event_bytes:
            raise ValueError("公开事件超过 max_event_bytes")
        started = monotonic()
        result = cast(
            list[object],
            await await_redis(
                self._write_client.eval(
                    _SCRIPT,
                    2,
                    self._keys.stream(ctx),
                    self._keys.metadata(ctx),
                    str(self._epoch_factory()),
                    encoded,
                    event.type,
                    int(self._clock().timestamp() * 1000),
                    self._config.retention_seconds,
                    self._config.terminal_grace_seconds,
                    self._config.max_events_per_session,
                )
            ),
        )
        cursor = PublicEventCursor(
            epoch=UUID(_to_text(result[1])),
            stream_id=_to_text(result[0]),
        )
        self._diagnose(
            operation="append",
            started=started,
            event_type=event.type,
            stream_length=_to_int(result[3]),
        )
        return PublicEventRecord(cursor=cursor, event=event)

    async def inspect(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
    ) -> StreamInspection:
        """读取 metadata 与首尾边界，并验证可恢复 cursor。

        Args:
            ctx: 目标 session 的隔离上下文。
            cursor: 可选的已消费位置。

        Returns:
            当前流边界；没有流且没有游标时返回不存在的快照。

        Raises:
            ServiceError: 游标过期、超前，或元数据与边界不一致。
        """
        metadata = await await_redis(
            self._read_client.hgetall(self._keys.metadata(ctx))
        )
        if not metadata:
            if cursor is not None:
                raise _cursor_error("cursor_expired", "cursor 所属 Stream 已过期")
            return StreamInspection(exists=False)
        epoch, lifecycle = _decode_metadata(metadata)
        first_raw = await await_redis(
            self._read_client.xrange(self._keys.stream(ctx), count=1)
        )
        last_raw = await await_redis(
            self._read_client.xrevrange(self._keys.stream(ctx), count=1)
        )
        if not first_raw or not last_raw:
            raise _cursor_error(
                "snapshot_required",
                "Stream metadata 与事件边界不一致",
            )
        first = PublicEventCursor(epoch=epoch, stream_id=_to_text(first_raw[0][0]))
        last = PublicEventCursor(epoch=epoch, stream_id=_to_text(last_raw[0][0]))
        self._validate_cursor(cursor, first=first, last=last)
        return StreamInspection(
            exists=True,
            epoch=epoch,
            lifecycle=lifecycle,
            first_cursor=first,
            last_cursor=last,
        )

    async def read(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
        *,
        count: int,
        block_ms: int,
    ) -> EventReadBatch:
        """使用有限 XREAD 从 cursor 之后读取，不保存 subscriber offset。

        Args:
            ctx: 目标 session 的隔离上下文。
            cursor: 已消费位置；None 从保留的开头读取。
            count: 最大记录数，必须为正数。
            block_ms: 阻塞上限毫秒数；零表示非阻塞。

        Returns:
            解码记录、下一游标及最新可获知的生命周期。

        Raises:
            ValueError: count 或 block_ms 无效。
            ServiceError: 游标校验失败或无法确定事件所属 epoch。
        """
        if count <= 0 or block_ms < 0:
            raise ValueError("count 必须为正数且 block_ms 不能为负数")
        started = monotonic()
        inspection = await self.inspect(ctx, cursor)
        redis_id = cursor.stream_id if cursor is not None else "0-0"
        response = await await_redis(
            self._read_client.xread(
                streams={self._keys.stream(ctx): redis_id},
                count=count,
                block=block_ms if block_ms > 0 else None,
            )
        )
        epoch = inspection.epoch
        if response and epoch is None:
            epoch = (await self.inspect(ctx, None)).epoch
        records = self._decode_response(response, epoch)
        next_cursor = records[-1].cursor if records else cursor
        lifecycle = await self._current_lifecycle(ctx, inspection.lifecycle)
        self._diagnose(
            operation="read",
            started=started,
            stream_length=len(records),
            outcome="empty" if not records else "ok",
        )
        return EventReadBatch(
            records=records,
            next_cursor=next_cursor,
            lifecycle=lifecycle,
        )

    def _encode(self, event: PublicStreamEvent) -> bytes:
        payload = _JSON_ADAPTER.validate_python(event.model_dump(mode="json"))
        return self._codec.encode(payload)

    def _decode_response(
        self,
        response: object,
        epoch: UUID | None,
    ) -> tuple[PublicEventRecord, ...]:
        if not response:
            return ()
        if epoch is None:
            raise _cursor_error("snapshot_required", "读取到事件但缺少 Stream epoch")
        records: list[PublicEventRecord] = []
        for _, entries in cast(
            list[tuple[object, list[tuple[object, Mapping[object, object]]]]], response
        ):
            for stream_id, fields in entries:
                normalized = {_to_text(key): value for key, value in fields.items()}
                raw_event = cast(bytes | str, normalized["event"])
                event = PublicStreamEvent.model_validate(self._codec.decode(raw_event))
                records.append(
                    PublicEventRecord(
                        cursor=PublicEventCursor(
                            epoch=epoch,
                            stream_id=_to_text(stream_id),
                        ),
                        event=event,
                    )
                )
        return tuple(records)

    async def _current_lifecycle(
        self,
        ctx: SessionContext,
        fallback: PublicStreamLifecycle | None,
    ) -> PublicStreamLifecycle | None:
        raw = await await_redis(
            self._read_client.hget(self._keys.metadata(ctx), "lifecycle")
        )
        return PublicStreamLifecycle(_to_text(raw)) if raw is not None else fallback

    @staticmethod
    def _validate_cursor(
        cursor: PublicEventCursor | None,
        *,
        first: PublicEventCursor,
        last: PublicEventCursor,
    ) -> None:
        if cursor is None:
            return
        if cursor.epoch != first.epoch:
            raise _cursor_error("cursor_expired", "cursor epoch 已失效")
        if _redis_id(cursor.stream_id) < _redis_id(first.stream_id):
            raise _cursor_error("cursor_expired", "cursor 已早于首条保留事件")
        if _redis_id(cursor.stream_id) > _redis_id(last.stream_id):
            raise _cursor_error("cursor_ahead", "cursor 超前于 Stream 尾部")

    def _diagnose(
        self,
        *,
        operation: str,
        started: float,
        event_type: str | None = None,
        stream_length: int | None = None,
        outcome: str = "ok",
    ) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            EventBusDiagnostic(
                operation=operation,
                duration_ms=(monotonic() - started) * 1000,
                event_type=event_type,
                stream_length=stream_length,
                outcome=outcome,
            )
        )


def _decode_metadata(
    raw: Mapping[object, object],
) -> tuple[UUID, PublicStreamLifecycle]:
    metadata = {_to_text(key): _to_text(value) for key, value in raw.items()}
    try:
        return UUID(metadata["epoch"]), PublicStreamLifecycle(metadata["lifecycle"])
    except (KeyError, ValueError) as error:
        raise _cursor_error("snapshot_required", "Stream metadata 非法") from error


def _redis_id(value: str) -> tuple[int, int]:
    milliseconds, sequence = value.split("-", 1)
    return int(milliseconds), int(sequence)


def _to_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError(f"Redis EventBus 值类型非法：{type(value).__name__}")


def _to_int(value: object) -> int:
    if isinstance(value, int):
        return value
    return int(_to_text(value))


def _cursor_error(code: str, message: str) -> ServiceError:
    """构造建立公开 Stream 前的稳定请求错误。"""
    kind = (
        ServiceErrorKind.CONFLICT
        if code in {"cursor_ahead", "snapshot_required"}
        else ServiceErrorKind.INVALID
    )
    return ServiceError(kind=kind, code=code, message=message)
