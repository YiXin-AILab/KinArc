"""Redis Pub/Sub 分布式取消广播与订阅生命周期。"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from agentsystem.cancellation.models import CancellationCommand
from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.redis.client import await_redis
from agentsystem.redis.codec import RedisJsonCodec

LOGGER = logging.getLogger(__name__)
_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]+")
_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_RECONNECT_INITIAL_SECONDS = 0.05
_RECONNECT_MAX_SECONDS = 1.0


class RedisCancellationBus:
    """使用共享 Redis client 广播取消命令，并命中本节点活动 Task。

    宿主负责启动和监督 run；close 请求停止，专用 PubSub 连接在 run 清理时
    关闭，共享 client 不由本对象关闭。Pub/Sub 不提供离线补投。
    """

    def __init__(
        self,
        client: Redis,
        *,
        prefix: str,
        registry: RunTaskRegistry,
    ) -> None:
        """保存共享连接与本地注册表，初始化时不建立订阅。

        Args:
            client: Application 管理生命周期的 Redis client。
            prefix: 取消频道的应用命名空间，仅允许字母数字、冒号、下划线和横线。
            registry: 将控制命令转换为本地 Task 取消的注册表。

        Raises:
            ValueError: prefix 为空或包含非法字符。
        """
        if _PREFIX_PATTERN.fullmatch(prefix) is None:
            raise ValueError("Redis prefix 包含非法字符")
        self._client = client
        self._channel = f"{prefix}:cancellation:commands"
        self._registry = registry
        self._codec = RedisJsonCodec()
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._pubsub: Any | None = None
        self._run_task: asyncio.Task[None] | None = None

    async def publish(self, command: CancellationCommand) -> int:
        """发布内部取消命令，不等待目标 run 收敛。

        Args:
            command: 带完整会话作用域和内部 run_id 的取消命令。

        Returns:
            Redis 报告的订阅接收者数量，不代表实际取消的 Task 数量。
        """
        payload = _JSON_ADAPTER.validate_python(command.model_dump(mode="json"))
        return int(
            await await_redis(
                self._client.publish(self._channel, self._codec.encode(payload))
            )
        )

    async def wait_ready(self) -> None:
        """等待首次 SUBSCRIBE 成功，防止服务过早接受请求。

        本方法没有内置等待期限，启动边界须提供 timeout。首次成功后即保持
        ready，后续重连不会清除该信号。
        """
        await self._ready.wait()

    async def run(self) -> None:
        """持续订阅并将消息转交本地注册表；连接故障按有界退避重连。

        close 触发的取消在此消费，其他 Task 取消继续传播。重连期间没有
        离线消息回放；非法单条消息记录警告后丢弃。

        Raises:
            RuntimeError: 未运行在 Task 中或已有订阅任务运行。
            Exception: 非连接类故障继续交给宿主监督。
        """
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("取消订阅必须运行在 asyncio Task 中")
        if self._run_task is not None and not self._run_task.done():
            raise RuntimeError("取消订阅已经运行")
        self._run_task = current_task
        delay = _RECONNECT_INITIAL_SECONDS
        try:
            while not self._closed.is_set():
                subscribed = await self._subscribe_once()
                if self._closed.is_set():
                    return
                if subscribed:
                    delay = _RECONNECT_INITIAL_SECONDS
                await self._wait_before_reconnect(delay)
                delay = min(delay * 2, _RECONNECT_MAX_SECONDS)
        except asyncio.CancelledError:
            if self._closed.is_set():
                current_task.uncancel()
                return
            raise
        finally:
            self._run_task = None

    async def _subscribe_once(self) -> bool:
        """运行一条连接生命周期，连接故障返回是否曾成功订阅。"""
        pubsub = self._client.pubsub()
        self._pubsub = pubsub
        subscribed = False
        try:
            await pubsub.subscribe(self._channel)
            subscribed = True
            self._ready.set()
            await self._consume(pubsub)
        except (RedisConnectionError, RedisTimeoutError, OSError) as error:
            if not self._closed.is_set():
                LOGGER.warning(
                    "取消订阅连接中断，准备重连 error_type=%s",
                    type(error).__name__,
                )
        finally:
            self._pubsub = None
            close = cast(Callable[[], Awaitable[None]], pubsub.aclose)
            await close()
        return subscribed

    async def _wait_before_reconnect(self, delay: float) -> None:
        """允许 close 立即打断固定上限的重连退避。"""
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=delay)
        except TimeoutError:
            return

    async def close(self) -> None:
        """请求停止订阅并取消运行任务，不等待清理完成。

        专用 PubSub 连接由 run 的 finally 路径关闭；共享连接池不受影响。
        关闭标记不重置，因此此实例不能重新启动。
        """
        self._closed.set()
        run_task = self._run_task
        if run_task is not None and run_task is not asyncio.current_task():
            run_task.cancel()

    async def _consume(self, pubsub: Any) -> None:
        """隔离单条坏消息，并只向 Registry 传递严格 scope。"""
        while not self._closed.is_set():
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message is None:
                continue
            try:
                command = self._decode(cast(dict[object, object], message))
            except (
                AdapterError,
                ValidationError,
                TypeError,
                KeyError,
            ) as error:
                LOGGER.warning(
                    "丢弃非法取消命令 error_type=%s",
                    type(error).__name__,
                )
                continue
            self._registry.request_if_active(
                SessionContext(
                    app_id=command.app_id,
                    agent_id=command.agent_id,
                    user_id=command.user_id,
                    session_id=command.session_id,
                ),
                command.run_id,
            )

    def _decode(self, message: dict[object, object]) -> CancellationCommand:
        """从 Redis message envelope 解码严格取消命令。"""
        data = message["data"] if "data" in message else message[b"data"]
        if not isinstance(data, (bytes, str)):
            raise TypeError("取消命令 payload 类型无效")
        return CancellationCommand.model_validate(self._codec.decode(data))
