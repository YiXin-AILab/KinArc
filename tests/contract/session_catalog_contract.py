"""Redis/MySQL 共用的对话管理行为合约。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.manager import MessageManager
from agentsystem.message.models import Message, MessageVisibility, Role, TextBlock
from agentsystem.message.sessions import SessionInfo, SessionListQuery, session_sort_key

NOW = datetime(2026, 9, 24, tzinfo=UTC)


class SessionCatalogContract:
    """由真实后端提供 manager/context 的共享测试。"""

    async def test_soft_delete_then_hard_delete(
        self, catalog: MessageManager, catalog_context: SessionContext
    ) -> None:
        ctx = catalog_context
        message = Message(
            message_id="delete-test",
            session_id=ctx.session_id,
            run_id="run",
            role=Role.USER,
            origin="user",
            visibility=MessageVisibility.ALL,
            content=(TextBlock(text="历史"),),
            created_at=NOW,
        )
        await catalog.commit(ctx, (message,))
        await catalog.delete_session(ctx)
        await catalog.delete_session(ctx)
        assert await catalog.get_session(ctx) is None
        assert (
            await catalog.list_sessions(
                SessionListQuery(app_id=ctx.app_id, user_id=ctx.user_id)
            )
        ).sessions == ()
        assert await catalog.list_messages(ctx) == (message,)
        with pytest.raises(AdapterError) as error:
            await catalog.ensure_session(ctx, title="复活", now=NOW)
        assert error.value.code == "session_deleted"
        assert await catalog.update_session_title(ctx, title="改名", now=NOW) is None
        await catalog.delete_session(ctx, hard_delete=True)
        await catalog.delete_session(ctx, hard_delete=True)
        assert await catalog.list_messages(ctx) == ()
        assert await catalog.get(ctx, message.message_id) is None
        with pytest.raises(AdapterError):
            await catalog.commit(ctx, (message,))

    async def test_delete_scope_isolation(
        self, catalog: MessageManager, catalog_context: SessionContext
    ) -> None:
        ctx = catalog_context
        info = await catalog.ensure_session(ctx, title="保留", now=NOW)
        await catalog.delete_session(ctx.model_copy(update={"user_id": "elsewhere"}))
        assert await catalog.get_session(ctx) == info

    async def test_hard_delete_blocks_in_flight_commit(
        self,
        catalog: MessageManager,
        catalog_context: SessionContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = catalog_context
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = catalog.ensure_session

        async def paused_ensure(
            ctx: SessionContext, *, title: str, now: datetime
        ) -> SessionInfo:
            result = await original(ctx, title=title, now=now)
            entered.set()
            await proceed.wait()
            return result

        monkeypatch.setattr(catalog, "ensure_session", paused_ensure)
        message = Message(
            message_id="late",
            session_id=ctx.session_id,
            run_id="run",
            role=Role.USER,
            origin="user",
            visibility=MessageVisibility.ALL,
            content=(TextBlock(text="迟到"),),
            created_at=NOW,
        )
        task = asyncio.create_task(catalog.commit(ctx, (message,)))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                await catalog.delete_session(ctx, hard_delete=True)
                proceed.set()
                with pytest.raises(AdapterError) as error:
                    await task
                assert error.value.code == "session_deleted"
            assert await catalog.list_messages(ctx) == ()
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_create_is_idempotent(
        self, catalog: MessageManager, catalog_context: SessionContext
    ) -> None:
        ctx = catalog_context
        first = await catalog.ensure_session(ctx, title="初始", now=NOW)
        again = await catalog.ensure_session(
            ctx, title="不同", now=NOW + timedelta(days=1)
        )
        assert again == first == await catalog.get_session(ctx)
        assert first.title_version == 1

    async def test_missing_and_empty(
        self, catalog: MessageManager, catalog_context: SessionContext
    ) -> None:
        ctx = catalog_context
        assert await catalog.get_session(ctx) is None
        assert await catalog.update_session_title(ctx, title="missing", now=NOW) is None
        page = await catalog.list_sessions(
            SessionListQuery(app_id=ctx.app_id, user_id=ctx.user_id)
        )
        assert page.sessions == ()
        assert page.next_cursor is None
        await catalog.commit(ctx, ())
        assert await catalog.get_session(ctx) is None

    async def test_pagination_and_scope(
        self, catalog: MessageManager, catalog_context: SessionContext
    ) -> None:
        ctx = catalog_context
        expected = []
        for index in range(5):
            item_ctx = ctx.model_copy(
                update={"session_id": str(index), "agent_id": "a" if index < 3 else "b"}
            )
            expected.append(
                await catalog.ensure_session(item_ctx, title=str(index), now=NOW)
            )
        await catalog.ensure_session(
            ctx.model_copy(update={"user_id": "other"}), title="private", now=NOW
        )
        await catalog.ensure_session(
            ctx.model_copy(update={"app_id": "other"}), title="private", now=NOW
        )
        query = SessionListQuery(app_id=ctx.app_id, user_id=ctx.user_id, limit=2)
        seen: list[SessionInfo] = []
        while True:
            page = await catalog.list_sessions(query)
            seen.extend(page.sessions)
            if page.next_cursor is None:
                break
            query = SessionListQuery(
                **(query.model_dump() | {"cursor": page.next_cursor})
            )
        assert seen == sorted(expected, key=session_sort_key, reverse=True)
        filtered = await catalog.list_sessions(
            SessionListQuery(app_id=ctx.app_id, user_id=ctx.user_id, agent_id="a")
        )
        assert len(filtered.sessions) == 3
        assert all(item.ctx.agent_id == "a" for item in filtered.sessions)

    async def test_rename_and_aba_conflict(
        self, catalog: MessageManager, catalog_context: SessionContext
    ) -> None:
        ctx = catalog_context
        first = await catalog.ensure_session(ctx, title="A", now=NOW)
        second = await catalog.update_session_title(
            ctx, title=" B ", now=NOW + timedelta(seconds=1)
        )
        assert second is not None and second.title == "B" and second.title_version == 2
        assert second.created_at == first.created_at
        third = await catalog.update_session_title(ctx, title="A", now=NOW)
        assert (
            third is not None
            and third.title_version == 3
            and third.updated_at == second.updated_at
        )
        with pytest.raises(AdapterError) as raised:
            await catalog.update_session_title(
                ctx, title="LLM", now=NOW, expected_version=1
            )
        assert raised.value.code == "session_title_conflict"
        assert await catalog.get_session(ctx) == third
        same = await catalog.update_session_title(
            ctx, title="A", now=NOW + timedelta(days=1)
        )
        assert same == third

    async def test_concurrent_create_and_conditional_rename(
        self, catalog: MessageManager, catalog_context: SessionContext
    ) -> None:
        ctx = catalog_context
        results = await asyncio.gather(
            *[
                catalog.ensure_session(ctx, title=str(index), now=NOW)
                for index in range(8)
            ]
        )
        assert all(item == results[0] for item in results)
        updates = await asyncio.gather(
            *[
                catalog.update_session_title(
                    ctx, title=f"title-{index}", now=NOW, expected_version=1
                )
                for index in range(8)
            ],
            return_exceptions=True,
        )
        assert sum(not isinstance(result, BaseException) for result in updates) == 1
        assert all(
            not isinstance(result, BaseException) or isinstance(result, AdapterError)
            for result in updates
        )
