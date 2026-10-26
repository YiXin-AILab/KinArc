"""Redis JSON payload codec 测试。"""

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.redis.codec import RedisJsonCodec


def test_codec_produces_canonical_bytes_for_equivalent_payloads() -> None:
    """键顺序不同但内容相同的 payload 必须产生相同 bytes。"""
    codec = RedisJsonCodec()

    first = codec.encode({"name": "agent", "nested": {"b": 2, "a": 1}})
    second = codec.encode({"nested": {"a": 1, "b": 2}, "name": "agent"})

    assert first == second
    assert codec.decode(first) == {
        "name": "agent",
        "nested": {"a": 1, "b": 2},
    }


def test_codec_rejects_unknown_schema_version() -> None:
    """未知 schema_version 不得被旧 Runtime 静默读取。"""
    encoded = b'{"payload":{"name":"agent"},"schema_version":2}'

    with pytest.raises(AdapterError) as captured:
        RedisJsonCodec().decode(encoded)

    assert captured.value.operation == "redis.decode"
    assert captured.value.code == "unsupported_schema_version"
