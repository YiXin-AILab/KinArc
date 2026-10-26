"""所有 MemoryStore 共享的行为与并发合约。"""

import asyncio

from agentsystem.memory.store import MemoryScope, MemoryStore


async def assert_memory_store_contract(store: MemoryStore, *, app_id: str) -> None:
    """校验创建、隔离、匹配及清空后追加。"""
    scope = MemoryScope.USER_GLOBAL
    assert (
        await store.read(app_id=app_id, scope=scope, user_id="u", agent_id=None) == ""
    )
    result = await store.add(
        app_id=app_id,
        scope=scope,
        user_id="u",
        agent_id=None,
        new_content="aa",
        max_chars=1_000_000,
    )
    assert result.content == "aa"
    result = await store.replace(
        app_id=app_id,
        scope=scope,
        user_id="u",
        agent_id=None,
        old_content="a",
        new_content="b",
        max_chars=1_000_000,
    )
    assert result.status == "text_ambiguous"
    result = await store.replace(
        app_id=app_id,
        scope=scope,
        user_id="u",
        agent_id=None,
        old_content="a",
        new_content="",
        replace_all=True,
        max_chars=1_000_000,
    )
    assert result.content == ""
    assert (
        await store.read(app_id=app_id, scope=scope, user_id="v", agent_id=None) == ""
    )
    assert (
        await store.read(
            app_id=app_id, scope=MemoryScope.USER_AGENT, user_id="u", agent_id="g"
        )
        == ""
    )


async def assert_concurrent_edits(
    first: MemoryStore, second: MemoryStore, *, app_id: str
) -> None:
    """两个 Store 的创建和不同片段替换不会丢失。"""
    scope = MemoryScope.USER_AGENT
    await asyncio.gather(
        *(
            store.add(
                app_id=app_id,
                scope=scope,
                user_id="u",
                agent_id="g",
                new_content=value,
                max_chars=1_000_000,
            )
            for store, value in ((first, "alpha"), (second, "beta"))
        )
    )
    await asyncio.gather(
        *(
            store.replace(
                app_id=app_id,
                scope=scope,
                user_id="u",
                agent_id="g",
                old_content=old,
                new_content=new,
                max_chars=1_000_000,
            )
            for store, old, new in ((first, "alpha", "A"), (second, "beta", "B"))
        )
    )
    content = await first.read(app_id=app_id, scope=scope, user_id="u", agent_id="g")
    assert set(content.splitlines()) == {"A", "B"}
    await first.add(
        app_id=app_id,
        scope=scope,
        user_id="quota",
        agent_id="g",
        new_content="123456",
        max_chars=10,
    )
    results = await asyncio.gather(
        *(
            store.add(
                app_id=app_id,
                scope=scope,
                user_id="quota",
                agent_id="g",
                new_content=value,
                max_chars=10,
            )
            for store, value in ((first, "ab"), (second, "cd"))
        )
    )
    assert sorted(result.status for result in results) == [
        "capacity_exceeded",
        "changed",
    ]
    saved = await first.read(app_id=app_id, scope=scope, user_id="quota", agent_id="g")
    assert len(saved) == 9
    refused = await first.replace(
        app_id=app_id,
        scope=scope,
        user_id="quota",
        agent_id="g",
        old_content="123456",
        new_content="1234567890",
        max_chars=10,
    )
    assert refused.status == "capacity_exceeded"
    assert (
        await first.read(app_id=app_id, scope=scope, user_id="quota", agent_id="g")
        == saved
    )
