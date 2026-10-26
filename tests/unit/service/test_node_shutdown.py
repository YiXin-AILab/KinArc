"""ServiceHost 关闭路径的故障注入测试。"""

import asyncio

import pytest
from agentsystem.config.models import RuntimeConfig
from agentsystem.service.agents import AgentRegistry
from agentsystem.service.host import ServiceHost, _create_asset_infrastructure
from agentsystem.trace.otel import OTelTraceRecorder
from opentelemetry.sdk.trace import TracerProvider
from tests.redis_config import local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)


class _Queue:
    async def initialize(self, agent_ids: tuple[str, ...]) -> None:
        assert agent_ids == ()
        return None


class _FailingDispatcher:
    def __init__(self) -> None:
        self.closed = False

    async def run(self) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("dispatcher failed")

    def close(self) -> None:
        self.closed = True


class _CancellationBus:
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()

    async def run(self) -> None:
        self.ready.set()
        await self.closed.wait()

    async def wait_ready(self) -> None:
        await self.ready.wait()

    async def close(self) -> None:
        self.closed.set()


class _TracerProvider(TracerProvider):
    def __init__(self) -> None:
        super().__init__()
        self.flushed = False
        self.shutdown_called = False

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        assert timeout_millis == 1_000
        self.flushed = True
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True


class _Redis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self, *, close_connection_pool: bool) -> None:
        assert close_connection_pool is True
        self.closed = True


async def test_dispatcher_failure_still_flushes_trace_and_closes_redis() -> None:
    """Dispatcher task 异常不能跳过后续基础设施清理。"""
    dispatcher = _FailingDispatcher()
    provider = _TracerProvider()
    recorder = OTelTraceRecorder(provider)
    redis = _Redis()
    assets = await _create_asset_infrastructure(
        RuntimeConfig(
            redis=local_redis_config(),
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
        ),
        TEST_PROJECT_ROOT,
    )
    service = ServiceHost(
        redis_client=redis,  # type: ignore[arg-type]
        owns_redis_client=True,
        inbox=_Queue(),  # type: ignore[arg-type]
        dispatcher=dispatcher,  # type: ignore[arg-type]
        cancellation_bus=_CancellationBus(),  # type: ignore[arg-type]
        application=object(),  # type: ignore[arg-type]
        registry=AgentRegistry(()),
        trace_recorder=recorder,
        asset_infrastructure=assets,
        shutdown_timeout_seconds=1,
    )
    await service.start()
    await asyncio.sleep(0)

    with pytest.raises(BaseExceptionGroup, match="关闭 ServiceHost 失败") as raised:
        await service.close()

    assert any("dispatcher failed" in str(error) for error in raised.value.exceptions)
    assert dispatcher.closed is True
    assert provider.flushed is True
    assert provider.shutdown_called is True
    assert redis.closed is True
