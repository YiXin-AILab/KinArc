"""MessageManager 内部的同槽 Redis 对话目录操作。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError
from redis.exceptions import RedisError

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.redis.keys import MessageRedisKeys
from agentsystem.message.sessions import (
    SessionInfo,
    SessionListQuery,
    SessionPage,
    SessionTitle,
    page_from_sessions,
    require_active_session,
    session_identity,
    session_sort_key,
)
from agentsystem.redis.client import RedisClient, await_redis

_SCRIPTS = Path(__file__).with_name("scripts")
_CREATE = (_SCRIPTS / "session_create.lua").read_text()
_LIST = (_SCRIPTS / "session_list.lua").read_text()
_RENAME = (_SCRIPTS / "session_rename.lua").read_text()
_DELETE = (_SCRIPTS / "session_delete.lua").read_text()
_TITLE: TypeAdapter[str] = TypeAdapter(SessionTitle)


class RedisSessionCatalog:
    """封装 RedisMessageManager 的用户对话目录及分页索引。

    复用管理器注入的 client 与 key 构造器，不管理连接生命周期。
    目录记录及用户、Agent 索引共用用户槽，与消息所在会话槽独立。
    """

    def __init__(self, client: RedisClient, keys: MessageRedisKeys) -> None:
        """接收 MessageManager 的共享依赖。

        Args:
            client: 由应用统一创建并负责关闭的异步 Redis client。
            keys: 与消息管理器共用的 key 构造器。
        """
        self._client = client
        self._keys = keys

    async def ensure(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        """同槽原子创建目录和两种索引；已有记录不变。

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
        # Fixed precision UTC strings allow Lua to compare update times.
        payload = info.model_dump(mode="json")
        payload["created_at"] = info.created_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        payload["updated_at"] = info.updated_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        raw = await self._eval(
            _CREATE,
            (
                self._keys.sessions(ctx.app_id, ctx.user_id),
                self._keys.session_index(ctx.app_id, ctx.user_id),
                self._keys.session_index(ctx.app_id, ctx.user_id, ctx.agent_id),
            ),
            (
                session_identity(ctx),
                json.dumps(payload, ensure_ascii=False),
                session_sort_key(info),
            ),
        )
        return require_active_session(self._decode(raw, ctx))

    async def get(
        self, ctx: SessionContext, *, include_deleted: bool = False
    ) -> SessionInfo | None:
        """读取并验证完整身份。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            include_deleted: 是否返回带删除标记的目录记录，默认隐藏。

        Returns:
            目录记录；不存在或被默认隐藏时为 None。

        Raises:
            AdapterError: 存储记录格式或完整会话身份校验失败。
        """
        try:
            raw = await await_redis(
                self._client.hget(
                    self._keys.sessions(ctx.app_id, ctx.user_id), session_identity(ctx)
                )
            )
        except RedisError as error:
            raise _storage_error() from error
        info = None if raw is None else self._decode(raw, ctx)
        return (
            None
            if info is not None and info.is_deleted and not include_deleted
            else info
        )

    async def delete(self, ctx: SessionContext) -> bool:
        """保留删除标记并从两个索引原子移除，不触碰其他用户。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            目录记录存在时返回 True；不存在时返回 False。

        Raises:
            AdapterError: 删除操作失败或已有目录数据非法。
        """
        info = await self.get(ctx, include_deleted=True)
        if info is None:
            return False
        await self._eval(
            _DELETE,
            (
                self._keys.sessions(ctx.app_id, ctx.user_id),
                self._keys.session_index(ctx.app_id, ctx.user_id),
                self._keys.session_index(ctx.app_id, ctx.user_id, ctx.agent_id),
            ),
            (session_identity(ctx), session_sort_key(info)),
        )
        return True

    async def list(self, query: SessionListQuery) -> SessionPage:
        """一次 Lua 读取有界且一致的一页记录。

        Args:
            query: 包含用户范围、可选 Agent、条数和游标的已校验查询。

        Returns:
            创建时间倒序的一页对话及可选下一页游标。

        Raises:
            AdapterError: 目录记录或索引与查询范围不匹配。
        """
        raw = await self._eval(
            _LIST,
            (
                self._keys.sessions(query.app_id, query.user_id),
                self._keys.session_index(query.app_id, query.user_id, query.agent_id),
            ),
            ("(" + query.before if query.before else "+", str(query.limit + 1)),
        )
        rows = cast(list[bytes | str], raw)
        items = []
        for position, payload in zip(rows[::2], rows[1::2], strict=True):
            info = self._decode(payload)
            key = position.decode() if isinstance(position, bytes) else position
            if (
                info.ctx.app_id != query.app_id
                or info.ctx.user_id != query.user_id
                or (query.agent_id is not None and info.ctx.agent_id != query.agent_id)
                or session_sort_key(info) != key
            ):
                raise _data_error()
            items.append(info)
        return page_from_sessions(query, tuple(items))

    async def rename(
        self,
        ctx: SessionContext,
        *,
        title: str,
        now: datetime,
        expected_version: int | None = None,
    ) -> SessionInfo | None:
        """原子校验版本并更新；不触碰列表索引或消息。

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
        raw = await self._eval(
            _RENAME,
            (self._keys.sessions(ctx.app_id, ctx.user_id),),
            (
                session_identity(ctx),
                title,
                now.astimezone(UTC).isoformat(timespec="microseconds"),
                str(expected_version) if expected_version is not None else "",
                ctx.app_id,
                ctx.user_id,
                ctx.agent_id,
                ctx.session_id,
            ),
        )
        if raw is None:
            return None
        result = cast(list[object], raw)[0]
        if result == -1:
            raise AdapterError(
                operation="session.rename",
                code="session_title_conflict",
                message="标题已修改，请重新生成",
            )
        if result == -2:
            raise _data_error()
        return self._decode(result, ctx)

    async def _eval(
        self, script: str, keys: tuple[str, ...], args: tuple[str, ...]
    ) -> object:
        try:
            return await await_redis(self._client.eval(script, len(keys), *keys, *args))
        except RedisError as error:
            raise _storage_error() from error

    @staticmethod
    def _decode(raw: object, ctx: SessionContext | None = None) -> SessionInfo:
        if not isinstance(raw, (bytes, str)):
            raise _data_error()
        try:
            info = SessionInfo.model_validate_json(raw)
        except ValidationError as error:
            raise _data_error() from error
        if ctx is not None and info.ctx != ctx:
            raise _data_error()
        return info


def _data_error() -> AdapterError:
    return AdapterError(
        operation="session.read",
        code="session_data_invalid",
        message="对话信息校验失败",
    )


def _storage_error() -> AdapterError:
    return AdapterError(
        operation="session.storage",
        code="session_storage_failed",
        message="对话存储不可用",
        retryable=True,
    )
