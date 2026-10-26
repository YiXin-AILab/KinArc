"""MessageManager 内部的 SQL 对话目录与标题事务。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import RowMapping, delete, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.sessions import (
    SessionInfo,
    SessionListQuery,
    SessionPage,
    SessionTitle,
    identity_digest,
    page_from_sessions,
    renamed_session,
    require_active_session,
    session_identity,
    session_sort_key,
)
from agentsystem.message.sql.schema import conversations, message_sessions, messages

_TITLE: TypeAdapter[str] = TypeAdapter(SessionTitle)


class SqlSessionCatalog:
    """封装 SqlMessageManager 的目录查询、标题更新和删除事务。

    复用管理器持有的 AsyncEngine 与超时设置，不创建或关闭 Engine。
    消息写入通过目录行锁与删除操作串行化，删除标记保留在目录中。
    """

    def __init__(self, engine: AsyncEngine, timeout: float) -> None:
        """保存调用方持有的连接设施。

        Args:
            engine: 应用持有并负责关闭的共享 AsyncEngine。
            timeout: 目录事务的等待上限，单位为秒。
        """
        self._engine = engine
        self._timeout = timeout

    async def ensure(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        """使用唯一键与 savepoint 幂等建立元数据。

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
        info = SessionInfo(ctx=ctx, title=title, created_at=now, updated_at=now)
        async with self._transaction() as connection:
            savepoint = await connection.begin_nested()
            try:
                await connection.execute(insert(conversations).values(**_values(info)))
                await savepoint.commit()
            except IntegrityError:
                await savepoint.rollback()
            row = (
                (
                    await connection.execute(
                        select(conversations)
                        .where(
                            conversations.c.scope_key
                            == bytes.fromhex(session_identity(ctx))
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            return require_active_session(_decode(row, ctx))

    async def get(self, ctx: SessionContext) -> SessionInfo | None:
        """按完整身份查询并校验存储值。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            活动目录记录；不存在或已删除时为 None。

        Raises:
            AdapterError: 存储记录格式或完整会话身份校验失败。
        """
        async with self._transaction() as connection:
            row = (
                (
                    await connection.execute(
                        select(conversations).where(
                            conversations.c.scope_key
                            == bytes.fromhex(session_identity(ctx))
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            return None if row is None or row["is_deleted"] else _decode(row, ctx)

    async def list(self, query: SessionListQuery) -> SessionPage:
        """沿用户或 Agent 复合索引查询有界的一页数据。

        Args:
            query: 包含用户范围、可选 Agent、条数和游标的已校验查询。

        Returns:
            创建时间倒序的一页对话及可选下一页游标。

        Raises:
            AdapterError: 目录记录或索引与查询范围不匹配。
        """
        column = (
            conversations.c.agent_key if query.agent_id else conversations.c.user_key
        )
        statement = select(conversations).where(
            column == bytes.fromhex(query.scope), conversations.c.is_deleted.is_(False)
        )
        if query.before:
            statement = statement.where(conversations.c.sort_key < query.before)
        statement = statement.order_by(conversations.c.sort_key.desc()).limit(
            query.limit + 1
        )
        async with self._transaction() as connection:
            rows = (await connection.execute(statement)).mappings().all()
            items = tuple(_decode(row) for row in rows)
            if any(
                item.ctx.app_id != query.app_id
                or item.ctx.user_id != query.user_id
                or (query.agent_id is not None and item.ctx.agent_id != query.agent_id)
                for item in items
            ):
                raise _data_error()
            return page_from_sessions(query, items)

    async def rename(
        self,
        ctx: SessionContext,
        *,
        title: str,
        now: datetime,
        expected_version: int | None = None,
    ) -> SessionInfo | None:
        """行锁内按版本更新标题，普通改名采用最后一次成功写入。

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
        title = _TITLE.validate_python(title)
        if now.tzinfo is None:
            raise ValueError("now 必须包含时区")
        condition = conversations.c.scope_key == bytes.fromhex(session_identity(ctx))
        async with self._transaction() as connection:
            row = (
                (
                    await connection.execute(
                        select(conversations).where(condition).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["is_deleted"]:
                return None
            previous = _decode(row, ctx)
            current = renamed_session(
                previous, title=title, now=now, expected_version=expected_version
            )
            if current != previous:
                await connection.execute(
                    update(conversations)
                    .where(condition)
                    .values(
                        title=current.title,
                        title_version=current.title_version,
                        updated_at=current.updated_at.astimezone(UTC).replace(
                            tzinfo=None
                        ),
                    )
                )
            return current

    async def delete(self, ctx: SessionContext, *, hard_delete: bool) -> None:
        """与写入共享目录行锁，原子隐藏目录并可选清空历史。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            hard_delete: 是否同时清空消息历史；两种删除均保留目录删除标记。

        Raises:
            AdapterError: 删除操作失败或已有目录数据非法。
        """
        key = bytes.fromhex(session_identity(ctx))
        async with self._transaction() as connection:
            row = (
                (
                    await connection.execute(
                        select(conversations)
                        .where(conversations.c.scope_key == key)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return
            _decode(row, ctx)
            await connection.execute(
                update(conversations)
                .where(conversations.c.scope_key == key)
                .values(is_deleted=True)
            )
            if hard_delete:
                await connection.execute(
                    delete(messages).where(messages.c.session_scope_key == key)
                )
                await connection.execute(
                    delete(message_sessions).where(message_sessions.c.scope_key == key)
                )

    async def require_active(
        self, connection: AsyncConnection, ctx: SessionContext
    ) -> None:
        """消息写事务持有目录行锁，防止删除之后提交迟到的消息。

        Args:
            connection: 调用方当前事务内的数据库连接。
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Raises:
            AdapterError: 目录身份非法或会话已删除。
        """
        row = (
            (
                await connection.execute(
                    select(conversations)
                    .where(
                        conversations.c.scope_key
                        == bytes.fromhex(session_identity(ctx))
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        require_active_session(_decode(row, ctx))

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        """统一限定整个事务的等待时间并隔离数据库异常。"""
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    yield connection
        except TimeoutError as error:
            raise AdapterError(
                operation="session.storage",
                code="adapter_timeout",
                message="对话存储超时",
                retryable=True,
            ) from error
        except SQLAlchemyError as error:
            raise AdapterError(
                operation="session.storage",
                code="session_storage_failed",
                message="对话存储不可用",
                retryable=True,
            ) from error


def _values(info: SessionInfo) -> dict[str, object]:
    ctx = info.ctx
    return {
        "scope_key": bytes.fromhex(session_identity(ctx)),
        "user_key": bytes.fromhex(identity_digest(ctx.app_id, ctx.user_id)),
        "agent_key": bytes.fromhex(
            identity_digest(ctx.app_id, ctx.user_id, ctx.agent_id)
        ),
        **ctx.model_dump(),
        "title": info.title,
        "title_version": info.title_version,
        "is_deleted": info.is_deleted,
        "created_at": info.created_at.astimezone(UTC).replace(tzinfo=None),
        "updated_at": info.updated_at.astimezone(UTC).replace(tzinfo=None),
        "sort_key": session_sort_key(info),
    }


def _decode(row: RowMapping, ctx: SessionContext | None = None) -> SessionInfo:
    try:
        stored_ctx = SessionContext(
            app_id=row["app_id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            session_id=row["session_id"],
        )
        info = SessionInfo(
            ctx=stored_ctx,
            title=row["title"],
            title_version=row["title_version"],
            is_deleted=row["is_deleted"],
            created_at=row["created_at"].replace(tzinfo=UTC),
            updated_at=row["updated_at"].replace(tzinfo=UTC),
        )
    except (ValidationError, AttributeError, TypeError) as error:
        raise _data_error() from error
    expected = _values(info)
    if (ctx is not None and info.ctx != ctx) or any(
        row[key] != expected[key]
        for key in ("scope_key", "user_key", "agent_key", "sort_key")
    ):
        raise _data_error()
    return info


def _data_error() -> AdapterError:
    return AdapterError(
        operation="session.read",
        code="session_data_invalid",
        message="对话信息校验失败",
    )
