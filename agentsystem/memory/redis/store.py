"""Python 文本编辑与 Lua 单键条件提交。"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from agentsystem.common.errors import AdapterError
from agentsystem.memory.redis.keys import memory_key
from agentsystem.memory.store import (
    MemoryEditResult,
    MemoryScope,
    append_content,
    replace_content,
)
from agentsystem.redis.client import RedisClient, await_redis

_SCRIPT = Path(__file__).with_name("scripts").joinpath("compare_write.lua").read_text()


class RedisMemoryStore:
    """通过 Redis 单键条件提交实现 MemoryStore。

    复用 Application 注入的 client，并关闭其网络自动重试，防止结果未知
    的追加被重放。在有限操作超时内，仅在 Lua 明确拒绝条件提交时重读
    和重算；不创建或关闭连接，不在存储层记录异常堆栈。
    """

    def __init__(
        self, client: RedisClient, *, prefix: str, timeout_seconds: float
    ) -> None:
        """保存资源，并关闭共享 client 的网络自动重试。

        Args:
            client: Application 创建和关闭的共享异步 Redis client。
            prefix: Application 分配的 Redis 命名空间。
            timeout_seconds: 一次完整操作的有限超时秒数。
        """
        self._client = client
        # 共享连接不能重放结果未知的非幂等追加；仅重试明确的条件冲突。
        client.set_retry(Retry(NoBackoff(), 0))
        self._prefix = prefix
        self._timeout = timeout_seconds

    async def read(
        self, *, app_id: str, scope: MemoryScope, user_id: str, agent_id: str | None
    ) -> str:
        """读取合法 UTF-8 正文，损坏记录报错。

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
        key = memory_key(
            self._prefix, app_id=app_id, scope=scope, user_id=user_id, agent_id=agent_id
        )
        try:
            async with asyncio.timeout(self._timeout):
                return await self._read(key)
        except (RedisError, TimeoutError, UnicodeError, TypeError) as error:
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
        """原子追加；未知提交结果不重试。

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
        key = memory_key(
            self._prefix, app_id=app_id, scope=scope, user_id=user_id, agent_id=agent_id
        )
        return await self._edit(
            key,
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
        """在最新正文上精确替换。

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
        key = memory_key(
            self._prefix, app_id=app_id, scope=scope, user_id=user_id, agent_id=agent_id
        )
        return await self._edit(
            key,
            lambda content: replace_content(
                content,
                old_content,
                new_content,
                replace_all=replace_all,
                max_chars=max_chars,
            ),
        )

    async def _read(self, key: str) -> str:
        value = await await_redis(self._client.get(key))
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            value.encode("utf-8")
            return value
        raise TypeError("Invalid memory record")

    async def _edit(
        self, key: str, edit: Callable[[str], MemoryEditResult]
    ) -> MemoryEditResult:
        """在同一个超时预算内重读、计算并条件提交。

        即便编辑结果是拒绝或无变化，也用 check 验证它对应的正文仍然有效；
        仅 Lua 明确返回条件冲突时重试，网络错误不重放可能已提交的操作。
        """
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    previous = await self._read(key)
                    result = edit(previous)
                    action = "write" if result.status == "changed" else "check"
                    accepted = cast(
                        object,
                        await await_redis(
                            self._client.eval(
                                _SCRIPT, 1, key, previous, action, result.content or ""
                            )
                        ),
                    )
                    if accepted == 1:
                        return result
                    if accepted != 0:
                        raise TypeError("Invalid memory commit result")
        except (RedisError, TimeoutError, UnicodeError, TypeError) as error:
            raise AdapterError(
                operation="memory.edit",
                message="Memory 写入结果未知，请先 memory_read 确认",
            ) from error
