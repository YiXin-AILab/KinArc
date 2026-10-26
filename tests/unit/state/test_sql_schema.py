"""SQL StateManager Core schema 与摘要键测试。"""

from agentsystem.common.models import SessionContext
from agentsystem.state.sql.keys import payload_hash, state_scope_key
from agentsystem.state.sql.schema import state_metadata, states
from sqlalchemy.dialects import mysql


def test_state_scope_key_uses_unambiguous_identity_segments() -> None:
    """不同身份分段不得因字符串拼接产生相同摘要。"""
    first = SessionContext(
        app_id="ab",
        user_id="c",
        agent_id="agent",
        session_id="session",
    )
    second = SessionContext(
        app_id="a",
        user_id="bc",
        agent_id="agent",
        session_id="session",
    )

    assert len(state_scope_key(first)) == 32
    assert state_scope_key(first) != state_scope_key(second)
    assert payload_hash(b"state") != payload_hash(b"other")


def test_state_schema_has_one_canonical_snapshot_per_scope() -> None:
    """每个 session 只能由一行固定摘要键保存最新快照。"""
    assert state_metadata.tables == {"runtime_states": states}
    assert states.name == "runtime_states"
    assert list(states.primary_key.columns.keys()) == ["scope_key"]
    dialect = mysql.dialect()
    assert states.c.scope_key.type.compile(dialect=dialect) == "BINARY(32)"
    assert states.c.payload_sha256.type.compile(dialect=dialect) == "BINARY(32)"
    assert isinstance(states.c.payload.type.dialect_impl(dialect), mysql.LONGBLOB)
    assert set(states.columns.keys()) == {
        "scope_key",
        "app_id",
        "user_id",
        "agent_id",
        "session_id",
        "payload",
        "payload_sha256",
    }
