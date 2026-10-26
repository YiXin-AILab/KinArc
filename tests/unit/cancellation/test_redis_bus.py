"""Redis Pub/Sub 取消广播 adapter 单元测试。"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from agentsystem.cancellation.models import CancellationCommand
from agentsystem.cancellation.redis_bus import RedisCancellationBus
from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.common.models import SessionContext
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

_CTX = SessionContext(
    app_id="app",
    agent_id="agent",
    user_id="user",
    session_id="session",
)
_COMMAND = CancellationCommand(
    **_CTX.model_dump(),
    run_id="run",
    requested_at=datetime(2026, 8, 13, tzinfo=UTC),
)


class _PubSub:
    def __init__(self) -> None:
        self.channel: str | None = None
        self.messages: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.channel = channel

    async def get_message(self, **kwargs: Any) -> dict[str, object] | None:
        del kwargs
        return await self.messages.get()

    async def aclose(self) -> None:
        self.closed = True
        self.messages.put_nowait(None)


class _Redis:
    def __init__(self) -> None:
        self.subscription = _PubSub()
        self.published: list[tuple[str, bytes]] = []

    def pubsub(self) -> _PubSub:
        return self.subscription

    async def publish(self, channel: str, payload: bytes) -> int:
        self.published.append((channel, payload))
        return 2


async def test_publish_uses_fixed_channel_and_subscriber_cancels_exact_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """广播只携带严格命令，非法正文隔离后订阅循环仍可处理下一条。"""
    client = _Redis()
    registry = RunTaskRegistry()
    target = asyncio.create_task(asyncio.Event().wait())
    registry.register(_CTX, "run", target)
    bus = RedisCancellationBus(
        cast(Redis, client),
        prefix="test-agent",
        registry=registry,
    )
    subscriber = asyncio.create_task(bus.run())
    await bus.wait_ready()

    assert await bus.publish(_COMMAND) == 2
    channel, encoded = client.published[0]
    assert channel == "test-agent:cancellation:commands"
    with caplog.at_level(logging.WARNING):
        await client.subscription.messages.put(
            {"type": "message", "data": b"private-invalid-payload"}
        )
        await client.subscription.messages.put({"type": "message", "data": encoded})
        with pytest.raises(asyncio.CancelledError):
            await target

    assert "private-invalid-payload" not in caplog.text
    await bus.close()
    await asyncio.wait_for(subscriber, timeout=1)
    assert client.subscription.closed is True


async def test_fatal_subscriber_error_is_not_hidden_by_reconnect() -> None:
    """非 Redis 连接异常必须退出 run，交给 ServiceHost 监督。"""

    class FailingPubSub(_PubSub):
        async def get_message(self, **kwargs: Any) -> dict[str, object] | None:
            del kwargs
            raise RuntimeError("subscriber bug")

    client = _Redis()
    client.subscription = FailingPubSub()
    bus = RedisCancellationBus(
        cast(Redis, client),
        prefix="test-agent",
        registry=RunTaskRegistry(),
    )

    with pytest.raises(RuntimeError, match="subscriber bug"):
        await bus.run()


async def test_connection_failure_reconnects_and_processes_later_command() -> None:
    """可恢复连接错误后必须重新订阅，而不是永久退出关键后台任务。"""

    class DisconnectedPubSub(_PubSub):
        async def get_message(self, **kwargs: Any) -> dict[str, object] | None:
            del kwargs
            raise RedisConnectionError("connection lost")

    class ReconnectingRedis(_Redis):
        def __init__(self) -> None:
            super().__init__()
            self.subscriptions = [DisconnectedPubSub(), _PubSub()]
            self.subscribe_count = 0
            self.reconnected = asyncio.Event()

        def pubsub(self) -> _PubSub:
            subscription = self.subscriptions[min(self.subscribe_count, 1)]
            self.subscribe_count += 1
            if self.subscribe_count >= 2:
                self.reconnected.set()
            return subscription

    client = ReconnectingRedis()
    registry = RunTaskRegistry()
    target = asyncio.create_task(asyncio.Event().wait())
    registry.register(_CTX, "run", target)
    bus = RedisCancellationBus(
        cast(Redis, client),
        prefix="test-agent",
        registry=registry,
    )
    subscriber = asyncio.create_task(bus.run())
    await bus.wait_ready()
    await asyncio.wait_for(client.reconnected.wait(), timeout=1)
    await bus.publish(_COMMAND)
    _, payload = client.published[-1]
    await client.subscriptions[1].messages.put({"type": "message", "data": payload})

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(target, timeout=1)

    await bus.close()
    await asyncio.wait_for(subscriber, timeout=1)
    assert client.subscribe_count >= 2
