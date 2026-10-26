"""StateManager 存储共享 codec 测试。"""

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.redis.codec import RedisJsonCodec
from agentsystem.runtime.state import AgentState
from agentsystem.state.persistence import StateCodec
from pydantic import TypeAdapter


def test_state_codec_preserves_existing_redis_bytes() -> None:
    """共享 codec 必须保持 Redis 已存 AgentState 字节兼容。"""
    state = AgentState(
        session_context=SessionContext(
            app_id="app",
            user_id="user",
            agent_id="agent",
            session_id="session",
        )
    )
    payload: JsonValue = TypeAdapter(JsonValue).validate_python(
        state.model_dump(mode="json")
    )

    encoded = StateCodec().encode(state)

    assert encoded == RedisJsonCodec().encode(payload)
    assert StateCodec().decode(encoded) == state


def test_state_codec_rejects_unknown_schema_version() -> None:
    """未知 schema version 必须收敛为 State 数据损坏错误。"""
    with pytest.raises(AdapterError) as captured:
        StateCodec().decode(b'{"payload":{},"schema_version":999}')
    assert captured.value.code == "unsupported_schema_version"
