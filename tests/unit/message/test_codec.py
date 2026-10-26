"""存储无关 Message codec 的版本边界测试。"""

import json
from datetime import UTC, datetime

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.persistence import MessageCodec


def test_message_codec_emits_schema_two() -> None:
    """新 Message 必须写成带 visibility 的 schema 2 envelope。"""
    message = Message(
        message_id="m-1",
        session_id="session",
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        content=(TextBlock(text="你好"),),
        visibility=MessageVisibility.ALL,
    )
    encoded = MessageCodec().encode(message)
    envelope = json.loads(encoded)

    assert envelope["schema_version"] == 2
    assert envelope["payload"]["visibility"] == "all"
    assert MessageCodec().decode(encoded) == message


def test_message_codec_rejects_schema_one_without_runtime_fallback() -> None:
    """Runtime 不得静默读取已废弃的 schema 1。"""
    encoded = json.dumps(
        {
            "schema_version": 1,
            "payload": {
                "message_id": "m-1",
                "session_id": "session",
                "role": "user",
                "content": [{"type": "text", "text": "old"}],
                "origin": "user",
                "run_id": "run-1",
                "created_at": "2026-08-10T00:00:00Z",
            },
        }
    )

    with pytest.raises(AdapterError) as captured:
        MessageCodec().decode(encoded)

    assert captured.value.code == "unsupported_schema_version"
