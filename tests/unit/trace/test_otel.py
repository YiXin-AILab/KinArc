"""具体 OpenTelemetry recorder 与 Provider 构造测试。"""

from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.config.models import RuntimeTraceConfig
from agentsystem.trace import otel
from agentsystem.trace.otel import OTelTraceRecorder, create_otel_provider
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

_NOW = datetime(2026, 8, 3, tzinfo=UTC)


class _RecordingProcessor(SpanProcessor):
    """Provider mapping 测试使用的无副作用 processor。"""

    def on_start(
        self,
        span: Span,
        parent_context: Context | None = None,
    ) -> None:
        del span, parent_context

    def on_end(self, span: ReadableSpan) -> None:
        del span

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


def test_provider_maps_yaml_config_to_explicit_sdk_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YAML 参数必须覆盖 SDK 中同名的遗留环境配置。"""
    captured: dict[str, object] = {}
    exporter = object()

    def create_exporter(**kwargs: object) -> object:
        captured["exporter"] = kwargs
        return exporter

    def create_processor(
        span_exporter: object,
        *,
        schedule_delay_millis: float | None = None,
    ) -> SpanProcessor:
        captured["processor_exporter"] = span_exporter
        captured["schedule_delay_millis"] = schedule_delay_millis
        return _RecordingProcessor()

    monkeypatch.setattr(otel, "OTLPSpanExporter", create_exporter)
    monkeypatch.setattr(otel, "BatchSpanProcessor", create_processor)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "legacy-service")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://legacy.invalid/v1/traces",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "Authorization=legacy-secret",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "99")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "99999")
    config = RuntimeTraceConfig(
        enabled=True,
        service_name="yaml-service",
        endpoint="http://yaml.test:4318/v1/traces",
        headers={"Authorization": "Basic yaml-secret"},
        timeout_seconds=7,
        batch_schedule_delay_ms=321,
    )

    provider = create_otel_provider(config)

    assert captured["exporter"] == {
        "endpoint": "http://yaml.test:4318/v1/traces",
        "headers": {"Authorization": "Basic yaml-secret"},
        "timeout": 7,
    }
    assert captured["processor_exporter"] is exporter
    assert captured["schedule_delay_millis"] == 321
    assert provider.resource.attributes["service.name"] == "yaml-service"


def test_recorder_preserves_parent_timing_attributes_and_error() -> None:
    """Recorder 必须保留父子关系、时间、属性和错误状态。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    recorder = OTelTraceRecorder(provider)

    assert recorder.start_span(key="root", name="invoke_agent", started_at=_NOW)
    assert recorder.start_span(
        key="child",
        parent_key="root",
        name="chat test-model",
        kind=SpanKind.CLIENT,
        started_at=_NOW + timedelta(milliseconds=1),
        attributes={"gen_ai.operation.name": "chat"},
    )
    assert recorder.end_span(
        key="child",
        ended_at=_NOW + timedelta(milliseconds=2),
        error_type="provider_error",
        error_description="LLM provider 返回错误",
        exception_type="agentsystem.common.errors.RunFailure",
        exception_message="LLM provider 返回错误",
        exception_stacktrace="Traceback ... private provider detail\n",
        exception_escaped=False,
    )
    assert recorder.end_span(
        key="root",
        ended_at=_NOW + timedelta(milliseconds=3),
    )

    child, root = exporter.get_finished_spans()
    assert child.parent is not None
    assert child.parent.span_id == root.context.span_id
    assert child.attributes is not None
    assert child.attributes["gen_ai.operation.name"] == "chat"
    assert child.attributes["error.type"] == "provider_error"
    assert child.status.status_code is StatusCode.ERROR
    assert child.status.description == "LLM provider 返回错误"
    exception_event = child.events[0]
    assert exception_event.name == "exception"
    assert exception_event.attributes is not None
    assert exception_event.attributes["exception.type"] == (
        "agentsystem.common.errors.RunFailure"
    )
    assert exception_event.attributes["exception.message"] == ("LLM provider 返回错误")
    assert exception_event.attributes["exception.stacktrace"] == (
        "Traceback ... private provider detail\n"
    )
    assert exception_event.attributes["exception.escaped"] == "False"


def test_recorder_start_and_end_are_idempotent() -> None:
    """相同业务 key 的重复开始和结束不会产生额外 Span。"""
    provider = TracerProvider()
    recorder = OTelTraceRecorder(provider)

    assert recorder.start_span(key="root", name="root", started_at=_NOW)
    assert recorder.start_span(key="root", name="root", started_at=_NOW) is False
    assert recorder.end_span(key="root", ended_at=_NOW) is True
    assert recorder.end_span(key="root", ended_at=_NOW) is False


def test_recorder_rejects_partial_exception_fields() -> None:
    """异常事件字段不完整时不得结束并损坏 Span。"""
    provider = TracerProvider()
    recorder = OTelTraceRecorder(provider)
    assert recorder.start_span(key="root", name="root", started_at=_NOW)

    with pytest.raises(ValueError, match="exception 字段必须同时提供"):
        recorder.end_span(
            key="root",
            ended_at=_NOW,
            exception_type="RuntimeError",
        )

    assert recorder.end_span(key="root", ended_at=_NOW)


def test_disabled_provider_records_no_finished_spans() -> None:
    """禁用策略使用 SDK sampler，不需要项目 Noop 实现。"""
    provider = create_otel_provider(RuntimeTraceConfig())
    span = provider.get_tracer("test").start_span("disabled")

    assert span.is_recording() is False
    assert provider.force_flush(timeout_millis=100)
