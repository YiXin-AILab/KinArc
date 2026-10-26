"""Runtime 独占的 OpenTelemetry Provider 与 Span recorder。"""

import asyncio
from collections.abc import Mapping
from datetime import datetime

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.util.types import AttributeValue

from agentsystem.config.models import RuntimeTraceConfig


def create_otel_provider(config: RuntimeTraceConfig) -> TracerProvider:
    """使用已校验配置创建当前 Runtime 独占的 Provider。

    不安装进程级全局 Provider；启用时创建 OTLP exporter 和批量处理器，
    由调用方负责刷新和关闭，禁用时使用不采样的 Provider。

    Args:
        config: Trace 开关、目标地址、凭证、服务名及批处理配置。

    Returns:
        新建的独立 TracerProvider。

    Raises:
        ValueError: 启用 Trace 却未提供 endpoint。
    """
    if not config.enabled:
        return TracerProvider(sampler=ALWAYS_OFF)
    endpoint = config.endpoint
    if endpoint is None:
        raise ValueError("启用 Trace 时 endpoint 必填")
    headers = {name: value.get_secret_value() for name, value in config.headers.items()}
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers,
        timeout=config.timeout_seconds,
    )
    provider = TracerProvider(resource=Resource({"service.name": config.service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            schedule_delay_millis=config.batch_schedule_delay_ms,
        )
    )
    return provider


class OTelTraceRecorder:
    """用稳定业务 key 管理当前进程内的 OTel Span 生命周期。

    持有注入的 Runtime 独占 Provider 和尚未结束的 Span，不设置全局 Provider。
    同一个活动 key 的重复开始被忽略；结束后可再次创建，例如挂起后的恢复。
    Application 应在退出时调用 shutdown 结束遗留 Span 并关闭 Provider。
    """

    def __init__(
        self,
        provider: TracerProvider,
        *,
        instrumentation_name: str = "agentsystem.runtime",
    ) -> None:
        """绑定 Runtime 独占 Provider，不修改进程全局 Provider。

        Args:
            provider: 由当前 Runtime 独占、关闭时一并 shutdown 的 Provider。
            instrumentation_name: 传给 Provider.get_tracer 的埋点模块名称。
        """
        self._provider = provider
        self._tracer: Tracer = provider.get_tracer(instrumentation_name)
        self._spans: dict[str, Span] = {}
        self._shutdown = False

    def start_span(
        self,
        *,
        key: str,
        name: str,
        started_at: datetime,
        parent_key: str | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> bool:
        """以显式 parent 开始唯一业务 key 对应的 Span。

        Args:
            key: 当前进程内用于关联开始和结束的业务标识。
            name: 展示给 Trace 后端的 Span 名称。
            started_at: 事件记录的开始时间。
            parent_key: 可选的活动父 Span 标识。
            kind: OTel Span 类型。
            attributes: 创建时附加的 OTel 属性。

        Returns:
            新建成功时为 True，活动 key 已存在时为 False。

        Raises:
            RuntimeError: recorder 已关闭。
            ValueError: 指定的父 Span 不存在。
        """
        if self._shutdown:
            raise RuntimeError("OTelTraceRecorder 已关闭")
        if key in self._spans:
            return False
        parent_context = None
        if parent_key is not None:
            parent = self._spans.get(parent_key)
            if parent is None:
                raise ValueError(f"Trace parent span 不存在：{parent_key}")
            parent_context = trace.set_span_in_context(parent)
        span = self._tracer.start_span(
            name,
            context=parent_context,
            kind=kind,
            attributes=dict(attributes or {}),
            start_time=_unix_nanos(started_at),
        )
        self._spans[key] = span
        return True

    def end_span(
        self,
        *,
        key: str,
        ended_at: datetime,
        attributes: Mapping[str, AttributeValue] | None = None,
        error_type: str | None = None,
        error_description: str | None = None,
        exception_type: str | None = None,
        exception_message: str | None = None,
        exception_stacktrace: str | None = None,
        exception_escaped: bool = False,
    ) -> bool:
        """补充属性并按事件时间结束业务 key 对应的 Span。

        Args:
            key: 待结束的活动业务标识。
            ended_at: 事件记录的结束时间。
            attributes: 结束前补充的 OTel 属性。
            error_type: 可选错误分类；存在时将 Span 标记为 ERROR。
            error_description: 错误状态描述。
            exception_type: 可选异常类型，须与 message、stacktrace 一起提供。
            exception_message: 异常事件消息。
            exception_stacktrace: 异常事件完整堆栈。
            exception_escaped: 异常是否逃离所记录操作。

        Returns:
            找到并结束 Span 时为 True，key 不存在时为 False。

        Raises:
            ValueError: 异常事件的类型、消息和堆栈未同时提供非空值。
        """
        exception_attributes = _exception_attributes(
            exception_type,
            exception_message,
            exception_stacktrace,
            exception_escaped,
        )
        span = self._spans.pop(key, None)
        if span is None:
            return False
        for attribute_name, value in (attributes or {}).items():
            span.set_attribute(attribute_name, value)
        if error_type is not None:
            span.set_attribute("error.type", error_type)
            span.set_status(Status(StatusCode.ERROR, error_description))
        if exception_attributes is not None:
            span.add_event(
                "exception",
                attributes=exception_attributes,
                timestamp=_unix_nanos(ended_at),
            )
        span.end(end_time=_unix_nanos(ended_at))
        return True

    async def force_flush(self, timeout_seconds: float) -> bool:
        """在线程中有限等待 Provider 刷新 exporter。

        Args:
            timeout_seconds: 正的等待上限，同时传给 SDK 刷新入口。

        Returns:
            SDK 报告的刷新是否完成。

        Raises:
            ValueError: 超时不是正数。
            TimeoutError: 异步等待超过限制；后台线程可能仍在结束刷新。
        """
        if timeout_seconds <= 0:
            raise ValueError("flush timeout 必须为正数")
        timeout_millis = max(1, int(timeout_seconds * 1_000))
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.to_thread(
                self._provider.force_flush,
                timeout_millis=timeout_millis,
            )

    async def shutdown(self, timeout_seconds: float) -> None:
        """结束遗留 Span 并有限等待 Provider 关闭。

        首次调用即标记已关闭，后续调用直接返回；超时不会重新启动关闭过程。

        Args:
            timeout_seconds: 等待同步 Provider.shutdown 的最大秒数。

        Raises:
            ValueError: 首次关闭时超时不是正数。
            TimeoutError: 等待超过限制，后台关闭线程可能仍在执行。
        """
        if self._shutdown:
            return
        if timeout_seconds <= 0:
            raise ValueError("shutdown timeout 必须为正数")
        self._shutdown = True
        for span in self._spans.values():
            span.end()
        self._spans.clear()
        async with asyncio.timeout(timeout_seconds):
            await asyncio.to_thread(self._provider.shutdown)


def _exception_attributes(
    exception_type: str | None,
    exception_message: str | None,
    exception_stacktrace: str | None,
    exception_escaped: bool,
) -> dict[str, AttributeValue] | None:
    fields = (exception_type, exception_message, exception_stacktrace)
    if all(field is None for field in fields):
        return None
    if not exception_type or not exception_message or not exception_stacktrace:
        raise ValueError("exception 字段必须同时提供非空值")
    return {
        "exception.type": exception_type,
        "exception.message": exception_message,
        "exception.stacktrace": exception_stacktrace,
        "exception.escaped": str(exception_escaped),
    }


def _unix_nanos(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)
