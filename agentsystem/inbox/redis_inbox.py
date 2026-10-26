"""Inbox 模块内聚的异步 Redis Inbox adapter。"""

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter
from redis.exceptions import ResponseError

from agentsystem.common.errors import AdapterError
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    NormalReady,
    ReadyEntry,
    ReadyKind,
    ResumeReady,
    ResumeRequest,
)
from agentsystem.inbox.redis_keys import InboxRedisKeys
from agentsystem.redis.client import RedisClient, await_redis
from agentsystem.redis.codec import RedisJsonCodec

_SCRIPT_DIR = Path(__file__).with_name("scripts")
_PUSH_SCRIPT = (_SCRIPT_DIR / "inbox_push.lua").read_text(encoding="utf-8")
_CLAIM_SCRIPT = (_SCRIPT_DIR / "inbox_claim.lua").read_text(encoding="utf-8")
_COMPLETE_SCRIPT = (_SCRIPT_DIR / "inbox_complete.lua").read_text(encoding="utf-8")
_LIST_SCRIPT = (_SCRIPT_DIR / "inbox_list.lua").read_text(encoding="utf-8")
_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_INBOX_SCHEMA_VERSION = 2
type RedisField = bytes | bytearray | memoryview[int] | str | int | float

LOGGER = logging.getLogger(__name__)


class RedisInbox:
    """以 Stream 传递 Ready、以 list/hash 保存输入和领取租约的 Redis 实现。

    使用共享 client，不拥有连接池；宿主启动时调用 initialize。普通输入写入
    与 Ready 追加是两个操作，不能保证跨两者的原子性；Ready 使用 NOACK。
    """

    def __init__(
        self,
        client: RedisClient,
        *,
        prefix: str,
        consumer_group: str = "runtime",
        max_messages: int = 1_000,
        inbox_ttl_seconds: int = 86_400,
        claim_ttl_seconds: int = 30,
    ) -> None:
        """保存共享 client 和队列边界，不创建 Consumer Group。

        Args:
            client: Application 管理的 Redis client。
            prefix: 当前应用的 Redis key 命名空间。
            consumer_group: Ready Stream 的共享消费组名称。
            max_messages: 单会话尚未完成消息数量上限。
            inbox_ttl_seconds: Inbox 数据过期秒数。
            claim_ttl_seconds: 单次领取租约的过期秒数。

        Raises:
            ValueError: 容量、TTL 非正数或 prefix 非法。
        """
        if max_messages <= 0 or inbox_ttl_seconds <= 0 or claim_ttl_seconds <= 0:
            raise ValueError("Inbox 容量和 TTL 必须为正数")
        self._client = client
        self._keys = InboxRedisKeys(prefix)
        self._consumer_group = consumer_group
        self._max_messages = max_messages
        self._inbox_ttl_seconds = inbox_ttl_seconds
        self._claim_ttl_milliseconds = claim_ttl_seconds * 1_000
        self._codec = RedisJsonCodec()

    async def initialize(self, agent_ids: Sequence[str]) -> None:
        """为节点支持的 Agent 幂等创建 Ready Stream Consumer Group。

        Args:
            agent_ids: Agent 标识；去重排序后处理，空序列不执行 I/O。

        Raises:
            ValueError: 存在空白 Agent 标识。
        """
        for agent_id in _normalized_agent_ids(agent_ids):
            await self._initialize_agent_stream(agent_id)

    async def _initialize_agent_stream(self, agent_id: str) -> None:
        """幂等初始化单个 Agent Ready Stream。"""
        try:
            await await_redis(
                self._client.xgroup_create(
                    self._keys.ready(agent_id),
                    self._consumer_group,
                    id="0",
                    mkstream=True,
                )
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
        """将普通输入按消息标识幂等写入 Inbox，再追加 NormalReady。

        Args:
            ctx: 目标会话的完整作用域。
            message: 普通输入，session_id 须与 ctx 匹配。

        重复的相同消息仍可产生 Ready；输入写入与 Ready 追加不构成一个事务。

        Raises:
            ValueError: 消息与作用域的 session_id 不匹配。
            AdapterError: 相同标识对应不同正文，或 Inbox 已满。
        """
        if message.session_id != ctx.session_id:
            raise ValueError("InboundMessage.session_id 与 SessionContext 不一致")
        payload = self._encode_message(message)
        result = cast(
            int,
            await await_redis(
                self._client.eval(
                    _PUSH_SCRIPT,
                    2,
                    self._keys.order(ctx),
                    self._keys.data(ctx),
                    message.message_id,
                    payload,
                    self._max_messages,
                    self._inbox_ttl_seconds,
                )
            ),
        )
        if result == -1:
            raise AdapterError(
                operation="inbox.push",
                code="inbox_message_conflict",
                message="相同 message_id 已存在不同 payload",
            )
        if result == -2:
            raise AdapterError(
                operation="inbox.push",
                code="inbox_capacity_exceeded",
                message="Inbox 已达到容量上限",
            )
        await self._append_ready(NormalReady(**ctx.model_dump()))

    async def push_resume(self, request: ResumeRequest) -> None:
        """追加恢复控制消息，不写普通 Inbox。

        Args:
            request: 包含已有 run 身份、单次 token 和恢复输入的请求。
        """
        ready = ResumeReady(
            **request.ctx.model_dump(),
            resume_id=request.resume_id,
            run_id=request.run_id,
            resume_token=request.resume_token,
            input=request.input,
        )
        await self._append_ready(ready)

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

        Raises:
            ValueError: count 非正数、block_ms 为负或 Agent 标识为空白。
        """
        if count <= 0 or block_ms < 0:
            raise ValueError("count 必须为正数且 block_ms 不能为负数")
        normalized_agent_ids = _normalized_agent_ids(agent_ids)
        if not normalized_agent_ids:
            return ()
        response = await await_redis(
            self._client.xreadgroup(
                self._consumer_group,
                consumer_id,
                {self._keys.ready(agent_id): ">" for agent_id in normalized_agent_ids},
                count=count,
                block=block_ms,
                noack=True,
            )
        )
        entries: list[ReadyEntry] = []
        for stream, stream_entries in response:
            for entry_id, fields in stream_entries:
                try:
                    entries.append(self._decode_ready(fields))
                except Exception as error:
                    log_unexpected_exception(
                        LOGGER,
                        "ready_decode_failed",
                        error,
                        operation="inbox.ready_decode",
                        stream=_redis_identifier(stream),
                        entry_id=_redis_identifier(entry_id),
                    )
        return tuple(entries)

    async def requeue_resume(self, ready: ResumeReady) -> None:
        """保持同一恢复 payload 重新追加控制消息。

        Args:
            ready: 锁忙时需要重投的 ResumeReady，保留 resume_id 和 token。
        """
        await self._append_ready(ready)

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

        Raises:
            ValueError: 非空 modes 下 owner_token 为空。
            AdapterError: 保存的 Inbox schema 版本不受支持。
        """
        if not modes:
            return ()
        if not owner_token:
            raise ValueError("owner_token 不能为空")
        encoded = await await_redis(
            self._client.eval(
                _CLAIM_SCRIPT,
                3,
                self._keys.order(ctx),
                self._keys.data(ctx),
                self._keys.claims(ctx),
                owner_token,
                self._claim_ttl_milliseconds,
                self._inbox_ttl_seconds,
                *(mode.value for mode in sorted(modes)),
            )
        )
        return tuple(self._decode_message(item) for item in encoded)

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

        Raises:
            ValueError: 非空消息列表中出现空标识，或 owner_token 为空。
            AdapterError: 领取所有权不匹配。
        """
        if not message_ids:
            return
        if not owner_token or any(not message_id for message_id in message_ids):
            raise ValueError("owner_token/message_id 不能为空")
        result = cast(
            int,
            await await_redis(
                self._client.eval(
                    _COMPLETE_SCRIPT,
                    3,
                    self._keys.order(ctx),
                    self._keys.data(ctx),
                    self._keys.claims(ctx),
                    owner_token,
                    *message_ids,
                )
            ),
        )
        if result == -1:
            raise AdapterError(
                operation="inbox.complete",
                code="inbox_claim_ownership_mismatch",
                message="Inbox claim owner 不匹配",
            )

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
        encoded = await await_redis(
            self._client.eval(
                _LIST_SCRIPT,
                2,
                self._keys.order(ctx),
                self._keys.data(ctx),
            )
        )
        return tuple(self._decode_message(item) for item in encoded)

    async def _append_ready(self, ready: ReadyEntry) -> None:
        fields: dict[RedisField, RedisField] = {
            "kind": ready.kind.value,
            "app_id": ready.app_id,
            "user_id": ready.user_id,
            "agent_id": ready.agent_id,
            "session_id": ready.session_id,
        }
        if isinstance(ready, ResumeReady):
            fields.update(
                {
                    "resume_id": ready.resume_id,
                    "run_id": ready.run_id,
                    "resume_token": ready.resume_token,
                    "input": self._codec.encode(
                        _JSON_ADAPTER.validate_python(ready.input)
                    ),
                }
            )
        await await_redis(self._client.xadd(self._keys.ready(ready.agent_id), fields))

    def _decode_ready(self, raw_fields: Mapping[object, object]) -> ReadyEntry:
        fields = {_to_text(key): value for key, value in raw_fields.items()}
        common = {
            "app_id": _to_text(fields["app_id"]),
            "user_id": _to_text(fields["user_id"]),
            "agent_id": _to_text(fields["agent_id"]),
            "session_id": _to_text(fields["session_id"]),
        }
        kind = _to_text(fields["kind"])
        if kind == ReadyKind.NORMAL:
            return NormalReady(**common)
        if kind != ReadyKind.RESUME:
            raise ValueError(f"未知 ReadyKind：{kind}")
        input_payload = self._codec.decode(cast(bytes | str, fields["input"]))
        return ResumeReady(
            **common,
            resume_id=_to_text(fields["resume_id"]),
            run_id=_to_text(fields["run_id"]),
            resume_token=_to_text(fields["resume_token"]),
            input=cast(Mapping[str, JsonValue], input_payload),
        )

    def _encode_message(self, message: InboundMessage) -> bytes:
        payload = _JSON_ADAPTER.validate_python(
            {
                "inbox_schema_version": _INBOX_SCHEMA_VERSION,
                "message": message.model_dump(mode="json"),
            }
        )
        return self._codec.encode(payload)

    def _decode_message(self, encoded: object) -> InboundMessage:
        """先验证 Inbox 信封版本，再校验领域消息；不兼容旧版存储结构。"""
        payload = self._codec.decode(cast(bytes | str, encoded))
        if not isinstance(payload, dict):
            raise AdapterError(
                operation="inbox.decode",
                code="unsupported_inbox_schema_version",
                message="不支持 Inbox schema_version=None",
            )
        version = payload.get("inbox_schema_version")
        if version != _INBOX_SCHEMA_VERSION:
            raise AdapterError(
                operation="inbox.decode",
                code="unsupported_inbox_schema_version",
                message=f"不支持 Inbox schema_version={version}",
            )
        return InboundMessage.model_validate(payload.get("message"))


def _to_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError(f"Redis Stream field 类型非法：{type(value).__name__}")


def _redis_identifier(value: object) -> str:
    """安全格式化 Redis stream/entry 标识，不记录业务 payload。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return f"<{type(value).__name__}>"


def _normalized_agent_ids(agent_ids: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(agent_ids)))
    if any(not agent_id.strip() for agent_id in normalized):
        raise ValueError("agent_id 不能为空")
    return normalized
