"""Redis session scope 编码测试。"""

from agentsystem.common.models import SessionContext
from agentsystem.inbox.redis_keys import InboxRedisKeys
from agentsystem.redis.scope import encode_redis_component, encode_session_scope
from redis.crc import key_slot


def test_redis_component_encoding_is_stable_and_delimiter_safe() -> None:
    """Agent Stream key component 不得泄漏分隔符、空格或 Unicode。"""
    encoded = encode_redis_component("Agent / 用户:one")

    assert encoded == encode_redis_component("Agent / 用户:one")
    assert all(value not in encoded for value in ("/", " ", ":", "用户", "="))


def test_session_fields_are_encoded_without_redis_key_delimiters() -> None:
    """分隔符、花括号和 Unicode 不得原样进入共享 scope。"""
    context = SessionContext(
        app_id="app:user",
        user_id="用户/{42}",
        agent_id="agent|one",
        session_id="session:with/slash",
    )

    scope = encode_session_scope(context)

    assert "app:user" not in scope
    assert "用户" not in scope
    assert "{" not in scope
    assert "}" not in scope


def test_distinct_field_boundaries_produce_distinct_scopes() -> None:
    """字段边界不同但拼接文本相似的 context 不得碰撞。"""
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

    assert encode_session_scope(first) != encode_session_scope(second)


def test_ready_keys_share_namespace_cluster_slot() -> None:
    """同一 Runtime namespace 的多 Agent Ready Stream 必须同槽。"""
    keys = InboxRedisKeys("agentsystem")

    first = keys.ready("agent-a")
    second = keys.ready("agent-b")

    assert key_slot(first.encode()) == key_slot(second.encode())
    assert "{agentsystem:ready}" in first


def test_ready_keys_from_different_namespaces_use_different_tags() -> None:
    """不同 Runtime namespace 不应被强制集中到同一 hash tag。"""
    first = InboxRedisKeys("runtime-a").ready("agent")
    second = InboxRedisKeys("runtime-b").ready("agent")

    assert "{runtime-a:ready}" in first
    assert "{runtime-b:ready}" in second
    assert key_slot(first.encode()) != key_slot(second.encode())
