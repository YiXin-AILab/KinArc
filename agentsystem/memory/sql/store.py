"""使用普通等号条件更新的 SQLAlchemy Core MemoryStore。"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentsystem.common.errors import AdapterError
from agentsystem.memory.sql.keys import MemoryIdentity, memory_identity
from agentsystem.memory.sql.schema import runtime_memories as table
from agentsystem.memory.store import (
    MemoryEditResult,
    MemoryScope,
    append_content,
    replace_content,
)


class SqlMemoryStore:
    """使用 SQLAlchemy Core 条件更新实现 MemoryStore。

    复用 Application 的 AsyncEngine，不建表、不迁移、不关闭连接池。
    编辑在有限超时内重试明确的唯一键或正文条件冲突，每次重试开启
    新事务；网络或提交失败不作为可安全重放的冲突处理。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        timeout_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存应用级资源，不执行迁移。

        Args:
            engine: Application 创建和关闭的共享 AsyncEngine。
            timeout_seconds: 一次完整操作的有限超时秒数。
            clock: 可注入的当前时间函数。
        """
        self._engine = engine
        self._timeout = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def read(
        self, *, app_id: str, scope: MemoryScope, user_id: str, agent_id: str | None
    ) -> str:
        """读取完整正文，缺失时返回空串。

        Args:
            app_id: 应用命名空间。
            scope: 用户记忆范围，Service 会校验是否启用。
            user_id: 当前用户身份。
            agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。

        Returns:
            完整记忆原文，记录不存在时为空字符串。

        Raises:
            AdapterError: 存储访问、解码或超时失败；写入报错时提交结果可能未知。
        """
        identity = memory_identity(
            app_id=app_id, scope=scope, user_id=user_id, agent_id=agent_id
        )
        try:
            async with (
                asyncio.timeout(self._timeout),
                self._engine.connect() as connection,
            ):
                row = await self._read(connection, identity)
                return row[1] if row else ""
        except (SQLAlchemyError, TimeoutError, UnicodeError, TypeError) as error:
            raise AdapterError(
                operation="memory.read", message="Memory 读取失败"
            ) from error

    async def add(
        self,
        *,
        app_id: str,
        scope: MemoryScope,
        user_id: str,
        agent_id: str | None,
        new_content: str,
        max_chars: int,
    ) -> MemoryEditResult:
        """创建或追加，身份唯一约束竞争后重读。

        Args:
            app_id: 应用命名空间。
            scope: 用户记忆范围，Service 会校验是否启用。
            user_id: 当前用户身份。
            agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。
            new_content: 含非空白字符的追加原文，已有正文时用单个换行分隔。
            max_chars: 最终完整正文的字符上限，按 Python len 计数。

        Returns:
            编辑结果；成功含完整正文，匹配或额度失败时正文为 None。

        Raises:
            AdapterError: 存储访问、解码或超时失败；写入报错时提交结果可能未知。
        """
        append_content("", new_content, max_chars=max_chars)
        identity = memory_identity(
            app_id=app_id, scope=scope, user_id=user_id, agent_id=agent_id
        )
        return await self._edit(
            identity,
            lambda content: append_content(content, new_content, max_chars=max_chars),
        )

    async def replace(
        self,
        *,
        app_id: str,
        scope: MemoryScope,
        user_id: str,
        agent_id: str | None,
        old_content: str,
        new_content: str,
        max_chars: int,
        replace_all: bool = False,
    ) -> MemoryEditResult:
        """精确替换，只重试确定未写入的条件冲突。

        Args:
            app_id: 应用命名空间。
            scope: 用户记忆范围，Service 会校验是否启用。
            user_id: 当前用户身份。
            agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。
            old_content: 精确匹配的非空原文，区分大小写和空白。
            new_content: 新正文片段；替换时空字符串表示删除。
            max_chars: 最终完整正文的字符上限，按 Python len 计数。
            replace_all: 是否替换全部非重叠匹配；否则要求唯一匹配起点。

        Returns:
            编辑结果；成功含完整正文，匹配或额度失败时正文为 None。

        Raises:
            AdapterError: 存储访问、解码或超时失败；写入报错时提交结果可能未知。
        """
        replace_content(
            "", old_content, new_content, replace_all=replace_all, max_chars=max_chars
        )
        identity = memory_identity(
            app_id=app_id, scope=scope, user_id=user_id, agent_id=agent_id
        )
        return await self._edit(
            identity,
            lambda content: replace_content(
                content,
                old_content,
                new_content,
                replace_all=replace_all,
                max_chars=max_chars,
            ),
        )

    async def _read(
        self, connection: AsyncConnection, identity: MemoryIdentity
    ) -> tuple[int, str] | None:
        condition = and_(*(table.c[name] == value for name, value in identity.items()))
        row = (
            await connection.execute(
                select(table.c.id, table.c.content).where(condition)
            )
        ).one_or_none()
        if row is None:
            return None
        if not isinstance(row.id, int) or not isinstance(row.content, str):
            raise TypeError("Invalid memory record")
        row.content.encode("utf-8")
        return row.id, row.content

    async def _edit(
        self, identity: MemoryIdentity, edit: Callable[[str], MemoryEditResult]
    ) -> MemoryEditResult:
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    async with self._engine.begin() as connection:
                        result = await self._attempt(connection, identity, edit)
                    if result is not None:
                        return result
        except (SQLAlchemyError, TimeoutError, UnicodeError, TypeError) as error:
            raise AdapterError(
                operation="memory.edit",
                message="Memory 写入结果未知，请先 memory_read 确认",
            ) from error

    async def _attempt(
        self,
        connection: AsyncConnection,
        identity: MemoryIdentity,
        edit: Callable[[str], MemoryEditResult],
    ) -> MemoryEditResult | None:
        """在当前事务尝试一次编辑，None 只表示确定可重试的写入冲突。

        新记录只恢复已识别的身份唯一约束冲突；已有记录通过旧正文条件更新。
        非 changed 结果直接返回，由外层事务和超时边界管理连接生命周期。
        """
        row = await self._read(connection, identity)
        previous = row[1] if row else ""
        result = edit(previous)
        if result.status != "changed":
            return result
        now = self._clock().astimezone(UTC).replace(tzinfo=None)
        if row is None:
            try:
                await connection.execute(
                    insert(table).values(
                        **identity,
                        content=result.content,
                        created_at=now,
                        updated_at=now,
                    )
                )
            except IntegrityError as error:
                if (
                    error.orig is None
                    or error.orig.args[0] != 1062
                    or "uq_runtime_memories_identity" not in str(error.orig)
                ):
                    raise
                await connection.rollback()
                return None
            return result
        written = await connection.execute(
            update(table)
            .where(
                table.c.id == row[0],
                table.c.content == previous,
                # utf8mb4_bin 忽略尾部空格，长度条件避免覆盖仅空格不同的并发正文。
                func.char_length(table.c.content) == len(previous),
            )
            .values(content=result.content, updated_at=now)
        )
        return result if written.rowcount == 1 else None
