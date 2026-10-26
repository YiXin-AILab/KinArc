"""基于 SQLAlchemy Core AsyncEngine 的 MessageManager 权威实现。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import Select

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.models import (
    CompactionBlock,
    ContextMessage,
    Message,
)
from agentsystem.message.persistence import MessageCodec, assemble_context_message
from agentsystem.message.sessions import SessionInfo, SessionListQuery, SessionPage
from agentsystem.message.sql.keys import message_key, payload_hash, session_scope_key
from agentsystem.message.sql.schema import message_sessions, messages
from agentsystem.message.sql.sessions import SqlSessionCatalog


@dataclass(frozen=True, slots=True)
class _EncodedMessage:
    """保存事务开始前已校验的消息与规范编码，避免持锁期间重复编码。

    Attributes:
        message: 原始不可变领域消息。
        key: message_id 的固定长度索引摘要。
        payload: 规范版本化 JSON bytes。
        payload_sha256: payload 的内容摘要，用于完整性及幂等校验。
    """

    message: Message
    key: bytes
    payload: bytes
    payload_sha256: bytes


class SqlMessageManager:
    """通过 SQLAlchemy Core 保存不可变消息与压缩指针。

    依赖应用持有的 AsyncEngine，不管理其生命周期，也不向 Redis 双写。
    消息事务锁定会话 head 分配稳定顺序；目录创建与消息提交分别执行。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        operation_timeout_seconds: float,
    ) -> None:
        """保存共享 Engine 和所有公开操作的有限等待时间。

        Args:
            engine: 应用持有并负责关闭的共享 AsyncEngine。
            operation_timeout_seconds: 每次数据库操作允许等待的秒数，必须大于零。

        Raises:
            ValueError: operation_timeout_seconds 不大于零。
        """
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds 必须大于 0")
        self._engine = engine
        self._operation_timeout_seconds = operation_timeout_seconds
        self._codec = MessageCodec()
        self._sessions = SqlSessionCatalog(engine, operation_timeout_seconds)

    async def ensure_session(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        """幂等创建对话目录。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

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

    async def delete_session(
        self, ctx: SessionContext, *, hard_delete: bool = False
    ) -> None:
        """删除对话目录，可选清空历史。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            hard_delete: 是否同时清空消息历史；两种删除均保留目录删除标记。

        Raises:
            AdapterError: 删除操作失败或已有目录数据非法。
        """
        await self._sessions.delete(ctx, hard_delete=hard_delete)

    async def get_session(self, ctx: SessionContext) -> SessionInfo | None:
        """读取对话基本信息。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            活动目录记录；不存在或已删除时为 None。

        Raises:
            AdapterError: 存储记录格式或完整会话身份校验失败。
        """
        return await self._sessions.get(ctx)

    async def list_sessions(self, query: SessionListQuery) -> SessionPage:
        """查询用户范围内的一页对话。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

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

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

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
        batch: tuple[Message, ...],
    ) -> None:
        """按输入首次出现顺序原子、幂等提交 Message 批次。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            batch: 按期望追加顺序排列的定稿消息；空批次不写入。

        Raises:
            ValueError: 消息 session_id 与 ctx 不一致。
            AdapterError: 同一消息 ID 内容冲突，或目标会话已删除。
        """
        encoded = self._prepare_batch(ctx, batch)
        if not encoded:
            return
        await self.ensure_session(ctx, title="新对话", now=batch[0].created_at)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    await self._sessions.require_active(connection, ctx)
                    head = await self._lock_session_head(connection, ctx)
                    await self._commit_locked(connection, ctx, head, encoded)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("message.commit") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("message.commit") from error

    async def commit_compaction(
        self,
        ctx: SessionContext,
        message: Message,
        *,
        expected_latest_compaction_id: str | None,
    ) -> None:
        """在 session 行锁内按预期上一版 ID 推进 Compaction 指针。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            message: 仅包含一个 CompactionBlock 的定稿消息。
            expected_latest_compaction_id: 预期上一版压缩消息 ID；None 表示尚无压缩。

        Raises:
            ValueError: 消息会话不匹配或不是压缩消息。
            AdapterError: 预期压缩指针冲突、同 ID 内容冲突或会话已删除。
        """
        if len(message.content) != 1 or not isinstance(
            message.content[0], CompactionBlock
        ):
            raise ValueError("commit_compaction 只接受 Compaction Message")
        prepared = self._prepare_batch(ctx, (message,))[0]
        await self.ensure_session(ctx, title="新对话", now=message.created_at)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    await self._sessions.require_active(connection, ctx)
                    head = await self._lock_session_head(connection, ctx)
                    await self._commit_compaction_locked(
                        connection,
                        ctx,
                        head,
                        prepared,
                        expected_latest_compaction_id,
                    )
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("message.commit_compaction") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("message.commit_compaction") from error

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        """使用一次带身份校验的 SELECT 获取 Message。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            message_id: 目标会话内的消息标识。

        Returns:
            已解码的目标消息；不存在时为 None。

        Raises:
            AdapterError: 消息封套或存储内容校验失败。
        """
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.connect() as connection:
                    row = (
                        (
                            await connection.execute(
                                self._message_select(ctx).where(
                                    messages.c.message_key == message_key(message_id)
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
            if row is None:
                return None
            self._validate_context_row(ctx, row)
            if row["message_id"] != message_id:
                raise _data_error(
                    "message.read", "message_key 对应的原始 message_id 不匹配"
                )
            return self._decode_context_row(ctx, row)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("message.read") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("message.read") from error

    async def list_messages(
        self,
        ctx: SessionContext,
    ) -> tuple[Message, ...]:
        """使用一次有序 SELECT 返回当前 session 的全部 Message。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            按首次提交顺序排列的全部已存消息；空会话返回空元组。

        Raises:
            AdapterError: 快照中的消息无法通过存储校验。
        """
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.connect() as connection:
                    rows = (
                        (
                            await connection.execute(
                                self._message_select(ctx).order_by(messages.c.position)
                            )
                        )
                        .mappings()
                        .all()
                    )
            decoded: list[Message] = []
            for row in rows:
                self._validate_context_row(ctx, row)
                decoded.append(self._decode_context_row(ctx, row))
            return tuple(decoded)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("message.list") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("message.list") from error

    async def read_context_source(self, ctx: SessionContext) -> ContextMessage:
        """以一次消息表有序 SELECT 组装一致的 ContextMessage。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            同一次存储读取组装的模型上下文源。

        Raises:
            AdapterError: 消息存储数据非法，或基础 SYSTEM 消息不是恰好一条。
        """
        scope_key = session_scope_key(ctx)
        statement = (
            select(
                messages.c.message_key,
                messages.c.message_id,
                messages.c.payload,
                messages.c.payload_sha256,
                messages.c.origin,
            )
            .where(messages.c.session_scope_key == scope_key)
            .order_by(messages.c.position)
        )
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.connect() as connection:
                    rows = (await connection.execute(statement)).mappings().all()
            decoded = tuple(self._decode_context_row(ctx, row) for row in rows)
            return assemble_context_message(decoded)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("message.read_context") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("message.read_context") from error

    def _prepare_batch(
        self,
        ctx: SessionContext,
        batch: tuple[Message, ...],
    ) -> tuple[_EncodedMessage, ...]:
        """在事务外编码整批消息，去重并保持 ID 首次出现的顺序。"""
        prepared_by_key: dict[bytes, _EncodedMessage] = {}
        for message in batch:
            if message.session_id != ctx.session_id:
                raise ValueError("Message.session_id 与 SessionContext 不匹配")
            payload = self._codec.encode(message)
            prepared = _EncodedMessage(
                message=message,
                key=message_key(message.message_id),
                payload=payload,
                payload_sha256=payload_hash(payload),
            )
            previous = prepared_by_key.get(prepared.key)
            if previous is None:
                prepared_by_key[prepared.key] = prepared
                continue
            if previous.message.message_id != message.message_id:
                raise _data_error(
                    "message.commit", "同一批次的 message_key 对应不同原始 message_id"
                )
            if previous.payload != payload:
                raise _conflict_error("message.commit")
        return tuple(prepared_by_key.values())

    async def _lock_session_head(
        self,
        connection: AsyncConnection,
        ctx: SessionContext,
    ) -> RowMapping:
        """先尝试创建 head，再取得行锁；冲突仅回滚 savepoint。"""
        scope_key = session_scope_key(ctx)
        savepoint = await connection.begin_nested()
        try:
            await connection.execute(
                insert(message_sessions).values(
                    scope_key=scope_key,
                    app_id=ctx.app_id,
                    user_id=ctx.user_id,
                    agent_id=ctx.agent_id,
                    session_id=ctx.session_id,
                    next_position=1,
                    latest_compaction_message_id=None,
                )
            )
            await savepoint.commit()
        except IntegrityError:
            await savepoint.rollback()
        row = (
            (
                await connection.execute(
                    select(message_sessions)
                    .where(message_sessions.c.scope_key == scope_key)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        self._validate_context_row(ctx, row)
        return row

    async def _commit_locked(
        self,
        connection: AsyncConnection,
        ctx: SessionContext,
        head: RowMapping,
        prepared: tuple[_EncodedMessage, ...],
    ) -> None:
        """在调用方持有 head 行锁时校验全部冲突，再连续分配新消息位置。"""
        scope_key = session_scope_key(ctx)
        rows = (
            (
                await connection.execute(
                    select(
                        messages.c.message_key,
                        messages.c.message_id,
                        messages.c.payload,
                        messages.c.payload_sha256,
                    ).where(
                        messages.c.session_scope_key == scope_key,
                        messages.c.message_key.in_(item.key for item in prepared),
                    )
                )
            )
            .mappings()
            .all()
        )
        existing = {bytes(row["message_key"]): row for row in rows}
        new_items: list[_EncodedMessage] = []
        for item in prepared:
            row = existing.get(item.key)
            if row is None:
                new_items.append(item)
                continue
            self._validate_existing_item(row, item)
        if not new_items:
            return
        next_position = int(head["next_position"])
        await connection.execute(
            insert(messages),
            [
                {
                    "session_scope_key": scope_key,
                    "message_key": item.key,
                    "message_id": item.message.message_id,
                    "position": next_position + offset,
                    "payload": item.payload,
                    "payload_sha256": item.payload_sha256,
                    "origin": item.message.origin,
                }
                for offset, item in enumerate(new_items)
            ],
        )
        await connection.execute(
            update(message_sessions)
            .where(message_sessions.c.scope_key == scope_key)
            .values(next_position=next_position + len(new_items))
        )

    async def _commit_compaction_locked(
        self,
        connection: AsyncConnection,
        ctx: SessionContext,
        head: RowMapping,
        item: _EncodedMessage,
        expected_latest_compaction_id: str | None,
    ) -> None:
        """在 head 行锁内识别幂等重试，并原子追加压缩消息与推进指针。"""
        scope_key = session_scope_key(ctx)
        existing = (
            (
                await connection.execute(
                    select(
                        messages.c.message_key,
                        messages.c.message_id,
                        messages.c.payload,
                        messages.c.payload_sha256,
                    ).where(
                        messages.c.session_scope_key == scope_key,
                        messages.c.message_key == item.key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        current_head = head["latest_compaction_message_id"]
        if current_head == item.message.message_id:
            if existing is None:
                raise _data_error(
                    "message.commit_compaction",
                    "Compaction head 指向不存在的 Message",
                )
            self._validate_existing_item(existing, item)
            return
        if existing is not None:
            if existing["message_id"] != item.message.message_id:
                raise _data_error(
                    "message.commit_compaction",
                    "message_key 对应的原始 message_id 不匹配",
                )
            raise _conflict_error("message.commit_compaction")
        if current_head != expected_latest_compaction_id:
            raise AdapterError(
                operation="message.commit_compaction",
                code="compaction_conflict",
                message="最新 Compaction 已变化",
            )
        next_position = int(head["next_position"])
        await connection.execute(
            insert(messages).values(
                session_scope_key=scope_key,
                message_key=item.key,
                message_id=item.message.message_id,
                position=next_position,
                payload=item.payload,
                payload_sha256=item.payload_sha256,
                origin=item.message.origin,
            )
        )
        await connection.execute(
            update(message_sessions)
            .where(message_sessions.c.scope_key == scope_key)
            .values(
                next_position=next_position + 1,
                latest_compaction_message_id=item.message.message_id,
            )
        )

    @staticmethod
    def _validate_existing_item(row: RowMapping, item: _EncodedMessage) -> None:
        if row["message_id"] != item.message.message_id:
            raise _data_error(
                "message.commit",
                "message_key 对应的原始 message_id 不匹配",
            )
        stored_hash = bytes(row["payload_sha256"])
        stored_payload = bytes(row["payload"])
        if stored_hash != payload_hash(stored_payload):
            raise _data_error("message.commit", "已存 Message payload hash 不匹配")
        if stored_hash == item.payload_sha256 and stored_payload == item.payload:
            return
        raise _conflict_error("message.commit")

    @staticmethod
    def _message_select(ctx: SessionContext) -> Select[tuple[object, ...]]:
        scope_key = session_scope_key(ctx)
        return (
            select(
                messages.c.message_key,
                messages.c.message_id,
                messages.c.payload,
                messages.c.payload_sha256,
                messages.c.origin,
                message_sessions.c.app_id,
                message_sessions.c.user_id,
                message_sessions.c.agent_id,
                message_sessions.c.session_id,
            )
            .select_from(
                messages.join(
                    message_sessions,
                    messages.c.session_scope_key == message_sessions.c.scope_key,
                )
            )
            .where(messages.c.session_scope_key == scope_key)
        )

    def _decode_row(self, row: RowMapping) -> Message:
        payload = bytes(row["payload"])
        if bytes(row["payload_sha256"]) != payload_hash(payload):
            raise _data_error("message.read", "已存 Message payload hash 不匹配")
        message = self._codec.decode(payload)
        if message.message_id != row["message_id"]:
            raise _data_error("message.read", "payload message_id 与索引列不匹配")
        if bytes(row["message_key"]) != message_key(message.message_id):
            raise _data_error("message.read", "payload message_id 与摘要键不匹配")
        if message.origin != row["origin"]:
            raise _data_error("message.read", "payload origin 与索引列不匹配")
        return message

    def _decode_context_row(self, ctx: SessionContext, row: RowMapping) -> Message:
        message = self._decode_row(row)
        if message.session_id != ctx.session_id:
            raise _data_error(
                "message.read", "payload session_id 与 SessionContext 不匹配"
            )
        return message

    @staticmethod
    def _validate_context_row(ctx: SessionContext, row: RowMapping) -> None:
        expected = (ctx.app_id, ctx.user_id, ctx.agent_id, ctx.session_id)
        actual = (row["app_id"], row["user_id"], row["agent_id"], row["session_id"])
        if actual != expected:
            raise _data_error(
                "message.read", "session_scope_key 对应的原始身份字段不匹配"
            )


def _conflict_error(operation: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        code="message_conflict",
        message="相同 message_id 对应不同内容",
    )


def _data_error(operation: str, message: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        code="message_payload_invalid",
        message=message,
    )


def _timeout_error(operation: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        code="adapter_timeout",
        message="数据库操作超时",
        retryable=True,
    )


def _storage_error(operation: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        message="数据库操作失败",
        retryable=True,
    )


__all__ = ["SqlMessageManager"]
