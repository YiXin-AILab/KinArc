"""通过单键 GET/SET 保存完整清单。"""

import asyncio
import base64
import json

from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.redis.client import RedisClient, await_redis
from agentsystem.todo.models import (
    TodoItem,
    decode_todos,
    encode_todos,
    validate_identity,
    validate_todos,
)


class RedisTodoStore:
    """用单键 GET/SET 实现当前 Todo 清单的权威存储。

    复用 Application client，构造时关闭其网络自动重试；每次 I/O
    受有限超时约束。完整清单无 TTL，损坏记录明确报错，未知写入结果
    不重放。连接创建和关闭均由 Application 负责。
    """

    def __init__(
        self, client: RedisClient, *, prefix: str, timeout_seconds: float
    ) -> None:
        """保存注入资源并关闭网络自动重试。

        Args:
            client: Application 创建和关闭的共享异步 Redis client。
            prefix: Application 分配的 Redis 命名空间。
            timeout_seconds: 一次完整操作的有限超时秒数。
        """
        self._client = client
        client.set_retry(Retry(NoBackoff(), 0))
        self._prefix = prefix
        self._timeout = timeout_seconds

    def _key(self, ctx: SessionContext) -> str:
        identity = json.dumps(validate_identity(ctx), ensure_ascii=False).encode()
        encoded = base64.urlsafe_b64encode(identity).decode("ascii")
        return f"{self._prefix}:todo:{encoded}"

    async def read(self, ctx: SessionContext) -> tuple[TodoItem, ...]:
        """读取并校验完整 JSON，不将损坏数据视为空。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。

        Returns:
            按原顺序返回的完整清单，不存在或已清空时为空。

        Raises:
            AdapterError: 存储访问、解码或超时失败；写入报错时提交结果可能未知。
        """
        key = self._key(ctx)
        try:
            async with asyncio.timeout(self._timeout):
                value = await await_redis(self._client.get(key))
                if value is None:
                    return ()
                if not isinstance(value, str | bytes):
                    raise TypeError("Invalid Todo record")
                return decode_todos(value)
        except (RedisError, TimeoutError, ValueError, TypeError) as error:
            raise AdapterError(
                operation="todo.read", message="Todo 读取失败"
            ) from error

    async def replace(
        self, ctx: SessionContext, todos: tuple[TodoItem, ...]
    ) -> tuple[TodoItem, ...]:
        """单条 SET 原子覆盖，无 TTL 和自动重试。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            todos: 按展示顺序排列的完整清单，空清单表示清空。

        Returns:
            校验后的完整条目序列；写入方法返回本次提交内容。

        Raises:
            AdapterError: 存储访问、解码或超时失败；写入报错时提交结果可能未知。
        """
        key = self._key(ctx)
        result = validate_todos(todos)
        encoded = encode_todos(result)
        try:
            async with asyncio.timeout(self._timeout):
                await await_redis(self._client.set(key, encoded))
        except (RedisError, TimeoutError) as error:
            raise AdapterError(
                operation="todo.replace",
                message="Todo 写入结果未知，请先 todo_read 确认",
            ) from error
        return result
