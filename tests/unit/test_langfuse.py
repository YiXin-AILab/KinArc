"""Langfuse 真实联调配置解析测试。"""

import base64

from tests.langfuse import parse_langfuse_otel_connection


def test_parse_langfuse_otel_connection_uses_standard_otel_values() -> None:
    """标准 OTLP endpoint 和 Basic header 应足够查询 Langfuse API。"""
    encoded = base64.b64encode(b"pk-lf-test:sk-lf-test").decode("ascii")

    connection = parse_langfuse_otel_connection(
        "http://127.0.0.1:3000/api/public/otel/v1/traces",
        f"Basic {encoded}",
    )

    assert connection == (
        "http://127.0.0.1:3000",
        "pk-lf-test",
        "sk-lf-test",
    )
