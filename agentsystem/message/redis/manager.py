"""MessageManager 模块内聚的异步 Redis adapter。"""

from datetime import datetime
from pathlib import Path
from typing import cast

from redis.exceptions import RedisError

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.models import (
    CompactionBlock,
    ContextMessage,
    Message,
)
from agentsystem.message.persistence import MessageCodec, assemble_context_message
from agentsystem.message.redis.keys import MessageRedisKeys
from agentsystem.message.redis.sessions import RedisSessionCatalog
from agentsystem.message.sessions import SessionInfo, SessionListQuery, SessionPage
from agentsystem.redis.client import RedisClient, await_redis

__all__ = ["RedisMessageManager"]


def _deleted_error() -> AdapterError:
    return AdapterError(
        operation="message.commit",
        code="session_deleted",
        message="对话已删除，请使用新的会话 ID",
    )


_SCRIPT_DIR = Path(__file__).with_name("scripts")
_COMMIT_SCRIPT = (_SCRIPT_DIR / "messages_commit.lua").read_text(encoding="utf-8")
_COMPACTION_SCRIPT = (_SCRIPT_DIR / "compaction_commit.lua").read_text(encoding="utf-8")
_READ_SCRIPT = (_SCRIPT_DIR / "messages_read.lua").read_text(encoding="utf-8")
_DELETE_SCRIPT = (_SCRIPT_DIR / "messages_delete.lua").read_text(encoding="utf-8")


class RedisMessageManager:
    """通过 Redis list/hash 和 Lua 保存不可变消息及压缩指针。

    依赖应用注入的异步 client，不创建或关闭连接池。消息写入在会话槽内
    原子执行；对话目录使用独立用户槽，目录与消息之间不构成单次事务。
    """

    def __init__(self, client: RedisClient, *, prefix: str) -> None:
        """接收共享异步 client；不拥有连接池。

        Args:
            client: 由应用统一创建并负责关闭的异步 Redis client。
            prefix: Redis namespace 前缀，仅允许字母、数字、冒号、下划线和连字符。

        Raises:
            ValueError: Redis prefix 为空或包含非法字符。
        """
        self._client = client
        self._keys = MessageRedisKeys(prefix)
        self._codec = MessageCodec()
        self._sessions = RedisSessionCatalog(client, self._keys)

    async def ensure_session(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        """幂等创建对话目录。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            title: 候选标题；创建已有会话时不覆盖其标题。
            now: 调用方提供的带时区时间，用于创建或更新时间。

        Returns:
            已有的活动目录记录或本次创建的记录；已有标题保持不变。

        Raises:
            AdapterError: 会话已删除或目录存储数据非法。
            ValueError: 标题或时间不满足目录模型约束。
        """
        return await self._sessions.ensure(ctx, title=title, now=now)

    async def get_session(self, ctx: SessionContext) -> SessionInfo | None:
        """读取对话基本信息。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            活动目录记录；不存在或已删除时为 None。

        Raises:
            AdapterError: 存储记录格式或完整会话身份校验失败。
        """
        return await self._sessions.get(ctx)

    async def delete_session(
        self, ctx: SessionContext, *, hard_delete: bool = False
    ) -> None:
        """先隐藏目录，再建立消息写屏障；跨槽失败可安全重试。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            hard_delete: 是否同时清空消息历史；两种删除均保留目录删除标记。

        Raises:
            AdapterError: 删除操作失败或已有目录数据非法。
        """
        try:
            if not await self._delete_directory(ctx):
                return
            await await_redis(
                self._client.eval(
                    _DELETE_SCRIPT,
                    4,
                    self._keys.order(ctx),
                    self._keys.data(ctx),
                    self._keys.latest_compaction(ctx),
                    self._keys.deleted(ctx),
                    "1" if hard_delete else "0",
                )
            )
        except RedisError as error:
            raise AdapterError(
                operation="session.delete",
                code="session_storage_failed",
                message="对话删除未完成，请重试",
                retryable=True,
            ) from error

    async def _delete_directory(self, ctx: SessionContext) -> bool:
        """删除目录；历史消息缺少目录时先回填，确保后续保留删除标记。"""
        if await self._sessions.delete(ctx):
            return True
        # Legacy Redis histories may not have a catalog record yet.
        first_id = await await_redis(self._client.lindex(self._keys.order(ctx), 0))
        if first_id is None:
            return False
        message_id = first_id.decode() if isinstance(first_id, bytes) else first_id
        first = await self.get(ctx, message_id)
        if first is None:
            raise AdapterError(
                operation="session.delete",
                code="session_data_invalid",
                message="历史消息索引缺少正文",
            )
        try:
            await self.ensure_session(ctx, title="新对话", now=first.created_at)
        except AdapterError as error:
            if error.code != "session_deleted":
                raise
        return await self._sessions.delete(ctx)

    async def list_sessions(self, query: SessionListQuery) -> SessionPage:
        """查询用户范围内的一页对话。

        Args:
            query: 包含用户范围、可选 Agent、条数和游标的已校验查询。

        Returns:
            创建时间倒序的一页对话及可选下一页游标。

        Raises:
            AdapterError: 目录记录或索引与查询范围不匹配。
        """
        return await self._sessions.list(query)

    async def update_session_title(
        self,
        ctx: SessionContext,
        *,
        title: str,
        now: datetime,
        expected_version: int | None = None,
    ) -> SessionInfo | None:
        """更新标题并可选校验版本。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            title: 要设置的标题，将按 SessionTitle 规则规范化。
            now: 调用方提供的带时区时间，用于创建或更新时间。
            expected_version: 预期标题版本；None 表示不做版本条件校验。

        Returns:
            改名后的目录记录；目标不存在或已删除时为 None。

        Raises:
            AdapterError: 预期标题版本冲突，或存储数据非法。
            ValueError: 标题非法或 now 不包含时区。
        """
        return await self._sessions.rename(
            ctx, title=title, now=now, expected_version=expected_version
        )

    async def commit(
        self,
        ctx: SessionContext,
        messages: tuple[Message, ...],
    ) -> None:
        """先检查整批冲突，再原子追加所有新 Message。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            messages: 按期望追加顺序排列的定稿消息；空批次不写入。

        Raises:
            ValueError: 消息 session_id 与 ctx 不一致。
            AdapterError: 同一消息 ID 内容冲突，或目标会话已删除。
        """
        arguments: list[str | bytes] = []
        batch_payloads: dict[str, bytes] = {}
        for message in messages:
            if message.session_id != ctx.session_id:
                raise ValueError("Message.session_id 与 SessionContext 不一致")
            payload = self._encode(message)
            existing = batch_payloads.get(message.message_id)
            if existing is not None and existing != payload:
                raise AdapterError(
                    operation="message.commit",
                    code="message_conflict",
                    message="相同 message_id 对应不同内容",
                )
            batch_payloads[message.message_id] = payload
        for message_id, payload in batch_payloads.items():
            arguments.extend((message_id, payload))
        if not arguments:
            return
        await self.ensure_session(ctx, title="新对话", now=messages[0].created_at)
        conflict: object = await await_redis(
            self._client.eval(
                _COMMIT_SCRIPT,
                3,
                self._keys.order(ctx),
                self._keys.data(ctx),
                self._keys.deleted(ctx),
                *arguments,
            )
        )
        if conflict == -1:
            raise _deleted_error()
        if conflict is not None:
            raise AdapterError(
                operation="message.commit",
                code="message_conflict",
                message="相同 message_id 对应不同内容",
            )

    async def commit_compaction(
        self,
        ctx: SessionContext,
        message: Message,
        *,
        expected_latest_compaction_id: str | None,
    ) -> None:
        """使用 Lua 原子校验上一版指针并幂等提交摘要。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            message: 仅包含一个 CompactionBlock 的定稿消息。
            expected_latest_compaction_id: 预期上一版压缩消息 ID；None 表示尚无压缩。

        Raises:
            ValueError: 消息会话不匹配或不是压缩消息。
            AdapterError: 预期压缩指针冲突、同 ID 内容冲突或会话已删除。
        """
        if message.session_id != ctx.session_id:
            raise ValueError("Message.session_id 与 SessionContext 不一致")
        if len(message.content) != 1 or not isinstance(
            message.content[0], CompactionBlock
        ):
            raise ValueError("commit_compaction 只接受 Compaction Message")
        await self.ensure_session(ctx, title="新对话", now=message.created_at)
        conflict: object = await await_redis(
            self._client.eval(
                _COMPACTION_SCRIPT,
                4,
                self._keys.order(ctx),
                self._keys.data(ctx),
                self._keys.latest_compaction(ctx),
                self._keys.deleted(ctx),
                expected_latest_compaction_id or "",
                message.message_id,
                self._encode(message),
            )
        )
        if conflict == -1:
            raise _deleted_error()
        if conflict is not None:
            raise AdapterError(
                operation="message.commit_compaction",
                code="compaction_conflict",
                message="最新 Compaction 已变化",
            )

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        """读取并校验单条 Message。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            message_id: 目标会话内的消息标识。

        Returns:
            已解码的目标消息；不存在时为 None。

        Raises:
            AdapterError: 消息封套或存储内容校验失败。
        """
        encoded = await await_redis(self._client.hget(self._keys.data(ctx), message_id))
        if encoded is None:
            return None
        return self._decode(encoded)

    async def list_messages(
        self,
        ctx: SessionContext,
    ) -> tuple[Message, ...]:
        """读取稳定快照中的全部 Message。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            按首次提交顺序排列的全部已存消息；空会话返回空元组。

        Raises:
            AdapterError: 快照中的消息无法通过存储校验。
        """
        return await self._read_all(ctx)

    async def read_context_source(self, ctx: SessionContext) -> ContextMessage:
        """从单次 Redis 快照构造 ContextMessage。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            同一次存储读取组装的模型上下文源。

        Raises:
            AdapterError: 消息存储数据非法，或基础 SYSTEM 消息不是恰好一条。
        """
        return assemble_context_message(await self._read_all(ctx))

    async def _read_all(self, ctx: SessionContext) -> tuple[Message, ...]:
        """在单次 Lua 执行中读取顺序及正文，避免分步读取产生撕裂快照。"""
        encoded = await await_redis(
            self._client.eval(
                _READ_SCRIPT,
                2,
                self._keys.order(ctx),
                self._keys.data(ctx),
            )
        )
        return tuple(self._decode(item) for item in cast(list[object], encoded))

    def _encode(self, message: Message) -> bytes:
        return self._codec.encode(message)

    def _decode(self, encoded: object) -> Message:
        return self._codec.decode(cast(bytes | str, encoded))


def _to_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError("Redis Message conflict ID 类型非法")
