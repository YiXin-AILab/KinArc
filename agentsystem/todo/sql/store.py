"""普通 SELECT/INSERT/UPDATE 实现的完整清单存储。"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.todo.models import (
    TodoItem,
    decode_todos,
    encode_todos,
    validate_identity,
    validate_todos,
)
from agentsystem.todo.sql.schema import runtime_todos as table


class SqlTodoStore:
    """使用共享 AsyncEngine 原子保存完整 Todo JSON 和管理时间。

    不自建连接池或迁移表结构，所有操作受有限超时约束。只有 INSERT
    明确返回主键重复时才在新事务覆盖一次；提交结果未知时不重放。
    存储不查询会话是否存在，也不自动清理清单。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        timeout_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """注入连接和 UTC 时钟，不自建连接池或执行迁移。

        Args:
            engine: Application 创建和关闭的共享 AsyncEngine。
            timeout_seconds: 一次完整操作的有限超时秒数。
            clock: 可注入的当前时间函数。
        """
        self._engine = engine
        self._timeout = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def read(self, ctx: SessionContext) -> tuple[TodoItem, ...]:
        """无记录返回空清单，损坏数据明确失败。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。

        Returns:
            按原顺序返回的完整清单，不存在或已清空时为空。

        Raises:
            AdapterError: 存储访问、解码或超时失败；写入报错时提交结果可能未知。
        """
        condition = _condition(ctx)
        try:
            async with asyncio.timeout(self._timeout), self._engine.connect() as conn:
                row = (
                    await conn.execute(select(table.c.todos_json).where(condition))
                ).one_or_none()
                if row is None:
                    return ()
                if not isinstance(row.todos_json, str):
                    raise TypeError("Invalid Todo record")
                return decode_todos(row.todos_json)
        except (SQLAlchemyError, TimeoutError, ValueError, TypeError) as error:
            raise AdapterError(
                operation="todo.read", message="Todo 读取失败"
            ) from error

    async def replace(
        self, ctx: SessionContext, todos: tuple[TodoItem, ...]
    ) -> tuple[TodoItem, ...]:
        """原子提交正文与时间，结果未知时不重放。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            todos: 按展示顺序排列的完整清单，空清单表示清空。

        Returns:
            校验后的完整条目序列；写入方法返回本次提交内容。

        Raises:
            AdapterError: 存储访问、解码或超时失败；写入报错时提交结果可能未知。
        """
        condition = _condition(ctx)
        result = validate_todos(todos)
        encoded = encode_todos(result)
        try:
            async with asyncio.timeout(self._timeout):
                await self._write(ctx, condition, encoded)
        except (SQLAlchemyError, TimeoutError) as error:
            raise AdapterError(
                operation="todo.replace",
                message="Todo 写入结果未知，请先 todo_read 确认",
            ) from error
        return result

    async def _write(
        self, ctx: SessionContext, condition: ColumnElement[bool], encoded: str
    ) -> None:
        """在事务内覆盖完整清单，首次插入的明确主键竞争仅恢复一次。"""
        # 只在 INSERT 本身明确返回主键重复时恢复；提交失败不进入恢复路径。
        duplicate = False
        async with self._engine.begin() as conn:
            exists = (
                await conn.execute(select(table.c.app_id).where(condition))
            ).first()
            now = self._clock().astimezone(UTC).replace(tzinfo=None)
            if exists is not None:
                await conn.execute(
                    update(table)
                    .where(condition)
                    .values(todos_json=encoded, updated_at=now)
                )
            else:
                try:
                    await conn.execute(
                        insert(table).values(
                            **ctx.model_dump(),
                            todos_json=encoded,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                except IntegrityError as error:
                    if not _duplicate_primary_key(error):
                        raise
                    await conn.rollback()
                    duplicate = True
        if duplicate:
            async with self._engine.begin() as conn:
                now = self._clock().astimezone(UTC).replace(tzinfo=None)
                await conn.execute(
                    update(table)
                    .where(condition)
                    .values(todos_json=encoded, updated_at=now)
                )


def _condition(ctx: SessionContext) -> ColumnElement[bool]:
    validate_identity(ctx)
    return and_(*(table.c[name] == value for name, value in ctx.model_dump().items()))


def _duplicate_primary_key(error: IntegrityError) -> bool:
    original = error.orig
    return bool(
        original
        and original.args
        and original.args[0] == 1062
        and "PRIMARY" in str(original)
    )
