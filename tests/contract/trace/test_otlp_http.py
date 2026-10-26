"""通用 OTLP/HTTP protobuf exporter 合约。"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Thread
from typing import ClassVar

import pytest
from agentsystem.config.models import RuntimeTraceConfig
from agentsystem.trace.otel import OTelTraceRecorder, create_otel_provider
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)


@dataclass(frozen=True, slots=True)
class ReceivedRequest:
    """本地 receiver 捕获的一次 OTLP HTTP 请求。"""

    path: str
    headers: Mapping[str, str]
    body: bytes


class _Handler(BaseHTTPRequestHandler):
    received: ClassVar[Queue[ReceivedRequest]]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """捕获 protobuf 请求并返回空的成功响应。"""
        length = int(self.headers["Content-Length"])
        self.received.put(
            ReceivedRequest(
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=self.rfile.read(length),
            )
        )
        response = ExportTraceServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        """关闭测试 receiver 的默认 stderr 日志。"""
        del format, args


type Receiver = tuple[ThreadingHTTPServer, Queue[ReceivedRequest]]


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    """启动只监听 loopback 的临时 OTLP receiver。"""
    received: Queue[ReceivedRequest] = Queue()
    _Handler.received = received
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("backend", "headers"),
    [
        ("jaeger", {}),
        ("tempo", {"x-scope-orgid": "test-tenant"}),
    ],
)
async def test_otlp_http_export_is_backend_neutral(
    backend: str,
    headers: dict[str, str],
    receiver: Receiver,
) -> None:
    """同一生产管线只需更换标准 endpoint 和 headers。"""
    server, received = receiver
    port = server.server_address[1]
    provider = create_otel_provider(
        RuntimeTraceConfig(
            enabled=True,
            service_name=f"{backend}-contract",
            endpoint=f"http://127.0.0.1:{port}/v1/traces",
            headers=headers,
            timeout_seconds=1,
            batch_schedule_delay_ms=10,
        )
    )
    recorder = OTelTraceRecorder(provider)
    now = datetime.now(UTC)

    assert recorder.start_span(key="root", name="invoke_agent", started_at=now)
    assert recorder.end_span(key="root", ended_at=now)
    assert await recorder.force_flush(2.0)
    request = received.get(timeout=1)

    assert request.path == "/v1/traces"
    assert request.headers["content-type"] == "application/x-protobuf"
    if backend == "tempo":
        assert request.headers["x-scope-orgid"] == "test-tenant"
    payload = ExportTraceServiceRequest()
    payload.ParseFromString(request.body)
    assert payload.resource_spans
    resource = payload.resource_spans[0].resource
    assert resource is not None
    resource_attributes = {
        item.key: item.value.string_value for item in resource.attributes
    }
    assert resource_attributes["service.name"] == f"{backend}-contract"
    await recorder.shutdown(2.0)
