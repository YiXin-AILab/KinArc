"""Memory 文本规则与进程内存储合约。"""

import asyncio
import tracemalloc

import pytest
from agentsystem.memory.local.store import LocalMemoryStore
from agentsystem.memory.store import MemoryScope, append_content, replace_content


def test_oversized_replacement_is_rejected_before_allocation() -> None:
    for new_content in ("b" * 2000, "b" * 20000):
        tracemalloc.start()
        try:
            result = replace_content(
                "a" * 2000, "a", new_content, replace_all=True, max_chars=2000
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert result.status == "capacity_exceeded"
        assert peak < 1_000_000


@pytest.mark.parametrize(
    ("content", "old", "new", "all_matches", "status", "result"),
    [
        ("aaa", "aa", "b", False, "text_ambiguous", None),
        ("aaa", "aa", "b", True, "changed", "ba"),
        ("aa", "a", "aa", True, "changed", "aaaa"),
        ("a-a", "a", "", True, "changed", "-"),
        ("x", "x", "x", False, "unchanged", "x"),
        ("xx", "x", "x", False, "text_ambiguous", None),
        ("X", "x", "y", False, "text_not_found", None),
        ("é", "e\u0301", "y", False, "text_not_found", None),
        (" x ", "x", "  ", False, "changed", "    "),
    ],
)
def test_replacement_rules(
    content: str,
    old: str,
    new: str,
    all_matches: bool,
    status: str,
    result: str | None,
) -> None:
    outcome = replace_content(content, old, new, replace_all=all_matches)
    assert (outcome.status, outcome.content) == (status, result)


def test_append_and_limits() -> None:
    assert append_content("", " a ").content == " a "
    assert append_content("a", "中").content == "a\n中"
    assert append_content("a" * 1_000_000, "b").status == "capacity_exceeded"
    assert replace_content("a", "a", "b" * 1_000_001).content is None
    for invalid in ("", " \n", "\ud800"):
        with pytest.raises(ValueError):
            append_content("", invalid)
    with pytest.raises(ValueError):
        replace_content("a", "", "x")


async def test_local_atomic_edits_and_isolation() -> None:
    store = LocalMemoryStore()
    scope = MemoryScope.USER_GLOBAL
    assert await store.read(app_id="a", scope=scope, user_id="u", agent_id=None) == ""
    await asyncio.gather(
        *(
            store.add(
                app_id="a",
                scope=scope,
                user_id="u",
                agent_id=None,
                new_content=str(index),
                max_chars=1_000_000,
            )
            for index in range(20)
        )
    )
    content = await store.read(app_id="a", scope=scope, user_id="u", agent_id=None)
    assert set(content.splitlines()) == {str(index) for index in range(20)}
    assert await store.read(app_id="a", scope=scope, user_id="v", agent_id=None) == ""
    assert await store.read(app_id="b", scope=scope, user_id="u", agent_id=None) == ""
    result = await store.replace(
        app_id="a",
        scope=scope,
        user_id="u",
        agent_id=None,
        old_content=content,
        new_content="",
        max_chars=1_000_000,
    )
    assert result.content == ""
    with pytest.raises(ValueError):
        await store.read(app_id="a", scope=scope, user_id="u", agent_id="g")


@pytest.mark.parametrize("field", ["app_id", "user_id", "agent_id"])
async def test_memory_rejects_trailing_space_identity(field: str) -> None:
    """独立 Memory 入口与 SessionContext 使用同样的身份规则。"""
    identity = dict(app_id="app", user_id="user", agent_id="agent")
    identity[field] += " "
    with pytest.raises(ValueError, match="trailing space"):
        await LocalMemoryStore().read(scope=MemoryScope.USER_AGENT, **identity)
