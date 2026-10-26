"""基于 SQLAlchemy Core AsyncEngine 的 StateManager 权威实现。"""

import asyncio

from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.runtime.state import AgentState
from agentsystem.state.manager import StateWriteResult
from agentsystem.state.persistence import StateCodec
from agentsystem.state.sql.keys import payload_hash, state_scope_key
from agentsystem.state.sql.schema import states


class SqlStateManager:
    """通过 SQLAlchemy Core 与行锁保存会话最新完整状态。

    依赖应用共享 AsyncEngine，不管理连接池生命周期，也不向 Redis 双写。
    规范快照与原始身份共同校验状态，有限超时覆盖数据库操作。
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
        self._codec = StateCodec()

    async def get(self, ctx: SessionContext) -> AgentState | None:
        """读取并校验 session 最新状态。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            已校验的最新状态；尚未创建时为 None。

        Raises:
            AdapterError: 存储状态无法解码或与请求身份不匹配。
        """
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.connect() as connection:
                    row = await self._select_row(connection, ctx, for_update=False)
            if row is None:
                return None
            return self._decode_row(ctx, row)[1]
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("state.read") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("state.read") from error

    async def create(self, initial_state: AgentState) -> StateWriteResult:
        """原子创建状态；相同规范快照可幂等重试。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            initial_state: 包含完整会话身份的初始执行快照。

        Returns:
            首次生效返回 APPLIED；已存相同目标快照返回 IDEMPOTENT。

        Raises:
            AdapterError: 已存在不同内容的状态，或存储内容非法。
        """
        ctx = initial_state.session_context
        payload = self._codec.encode(initial_state)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    savepoint = await connection.begin_nested()
                    try:
                        await connection.execute(
                            insert(states).values(
                                scope_key=state_scope_key(ctx),
                                app_id=ctx.app_id,
                                user_id=ctx.user_id,
                                agent_id=ctx.agent_id,
                                session_id=ctx.session_id,
                                payload=payload,
                                payload_sha256=payload_hash(payload),
                            )
                        )
                        await savepoint.commit()
                        return StateWriteResult.APPLIED
                    except IntegrityError:
                        await savepoint.rollback()
                    row = await self._select_row(connection, ctx, for_update=True)
                    if row is None:
                        raise _data_error(
                            "state.create",
                            "State 唯一键冲突后未找到已有行",
                        )
                    stored_payload, _ = self._decode_row(ctx, row)
                    if stored_payload == payload:
                        return StateWriteResult.IDEMPOTENT
                    raise _conflict_error("state.create")
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("state.create") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("state.create") from error

    async def save(
        self,
        previous: AgentState,
        current: AgentState,
    ) -> StateWriteResult:
        """在行锁内通过完整 previous 快照 CAS 保存 current。

        数据库异常和超时转换为 AdapterError，取消继续向上传播。

        Args:
            previous: CAS 预期的完整旧执行快照。
            current: 拟保存的完整新执行快照，必须与旧快照属于同一会话。

        Returns:
            首次生效返回 APPLIED；已存相同目标快照返回 IDEMPOTENT。

        Raises:
            ValueError: previous 与 current 的会话身份不同。
            AdapterError: 状态不存在、旧快照不匹配或存储内容非法。
        """
        if previous.session_context != current.session_context:
            raise ValueError("previous/current SessionContext 不一致")
        ctx = current.session_context
        previous_payload = self._codec.encode(previous)
        current_payload = self._codec.encode(current)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    row = await self._select_row(connection, ctx, for_update=True)
                    if row is None:
                        raise _conflict_error("state.save")
                    stored_payload, _ = self._decode_row(ctx, row)
                    if stored_payload == current_payload:
                        return StateWriteResult.IDEMPOTENT
                    if stored_payload != previous_payload:
                        raise _conflict_error("state.save")
                    await connection.execute(
                        update(states)
                        .where(states.c.scope_key == state_scope_key(ctx))
                        .values(
                            payload=current_payload,
                            payload_sha256=payload_hash(current_payload),
                        )
                    )
                    return StateWriteResult.APPLIED
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout_error("state.save") from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage_error("state.save") from error

    @staticmethod
    async def _select_row(
        connection: AsyncConnection,
        ctx: SessionContext,
        *,
        for_update: bool,
    ) -> RowMapping | None:
        statement = select(states).where(states.c.scope_key == state_scope_key(ctx))
        if for_update:
            statement = statement.with_for_update()
        return (await connection.execute(statement)).mappings().one_or_none()

    def _decode_row(
        self,
        ctx: SessionContext,
        row: RowMapping,
    ) -> tuple[bytes, AgentState]:
        """先校验索引身份及 payload 摘要，再校验领域快照内的完整身份。"""
        self._validate_identity(ctx, row)
        payload = bytes(row["payload"])
        if bytes(row["payload_sha256"]) != payload_hash(payload):
            raise _data_error("state.read", "已存 AgentState payload hash 不匹配")
        state = self._codec.decode(payload)
        if state.session_context != ctx:
            raise _data_error(
                "state.read",
                "AgentState SessionContext 与索引身份不匹配",
            )
        return payload, state

    @staticmethod
    def _validate_identity(ctx: SessionContext, row: RowMapping) -> None:
        expected = (ctx.app_id, ctx.user_id, ctx.agent_id, ctx.session_id)
        actual = (row["app_id"], row["user_id"], row["agent_id"], row["session_id"])
        if actual != expected:
            raise _data_error(
                "state.read",
                "state scope key 对应的原始身份字段不匹配",
            )


def _conflict_error(operation: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        code="state_conflict",
        message="state CAS 前置条件不成立",
    )


def _data_error(operation: str, message: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        code="state_payload_invalid",
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


__all__ = ["SqlStateManager"]
