"""ApplicationService 的对话元数据与标题生成编排测试。"""

from dataclasses import replace
from datetime import datetime

import pytest
from agentsystem.common.errors import AdapterError, ServiceError
from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import InboundMessage, InboundRole
from agentsystem.message.models import Message, TextBlock
from agentsystem.message.sessions import (
    SessionInfo,
    SessionListQuery,
    SessionPage,
    page_from_sessions,
    renamed_session,
    session_sort_key,
)
from agentsystem.service.agents import AgentRegistry
from agentsystem.service.application import (
    ApplicationService as RuntimeApplicationService,
)
from agentsystem.service.models import SubmitMessageCommand
from tests.unit.service.test_titles import NOW, TitleProvider, _client, message
from tests.unit.test_application import (
    _CONTEXT,
    ApplicationService,
    _Agents,
    _Messages,
    _Queue,
    _States,
)


class CatalogMessages(_Messages):
    """只为本测试实现元数据，消息读写沿用已有场景 fixture。"""

    def __init__(self) -> None:
        super().__init__((message("用户问题"),))
        self.sessions: dict[str, SessionInfo] = {}
        self.fail_ensure = False

    async def ensure_session(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        if self.fail_ensure:
            raise AdapterError(operation="session.create", message="存储不可用")
        key = ctx.model_dump_json()
        if key not in self.sessions:
            self.sessions[key] = SessionInfo(
                ctx=ctx, title=title, created_at=now, updated_at=now
            )
        return self.sessions[key]

    async def get_session(self, ctx: SessionContext) -> SessionInfo | None:
        return self.sessions.get(ctx.model_dump_json())

    async def list_sessions(self, query: SessionListQuery) -> SessionPage:
        items = tuple(
            sorted(
                (
                    item
                    for item in self.sessions.values()
                    if item.ctx.app_id == query.app_id
                    and item.ctx.user_id == query.user_id
                    and (query.agent_id is None or item.ctx.agent_id == query.agent_id)
                    and (query.before is None or session_sort_key(item) < query.before)
                ),
                key=session_sort_key,
                reverse=True,
            )
        )
        return page_from_sessions(query, items)

    async def update_session_title(
        self,
        ctx: SessionContext,
        *,
        title: str,
        now: datetime,
        expected_version: int | None = None,
    ) -> SessionInfo | None:
        old = await self.get_session(ctx)
        if old is None:
            return None
        new = renamed_session(
            old, title=title, now=now, expected_version=expected_version
        )
        self.sessions[ctx.model_dump_json()] = new
        return new


def service(
    catalog: CatalogMessages,
    provider: TitleProvider | None = None,
    queue: _Queue | None = None,
) -> RuntimeApplicationService:
    bundle = _Agents().require("agent")
    registry = AgentRegistry(
        (replace(bundle, llm_client=_client(provider or TitleProvider())),)
    )
    return ApplicationService(
        agents=registry,
        inbox=queue or _Queue(),
        states=_States(None),
        messages=catalog,
        clock=lambda: NOW,
    )


async def test_submit_creates_16_character_title_and_preserves_rename() -> None:
    catalog = CatalogMessages()
    app = service(catalog)
    command = SubmitMessageCommand(
        message_id="m",
        role=InboundRole.USER,
        origin="user",
        content=(TextBlock(text="中" * 17),),
    )
    await app.submit_message(_CONTEXT, command)
    info = await catalog.get_session(_CONTEXT)
    assert info is not None and info.title == "中" * 16 + "..."
    await app.update_session_title(_CONTEXT, "人工标题")
    await app.submit_message(_CONTEXT, command)
    assert (
        await app.list_sessions(SessionListQuery(app_id="app", user_id="user"))
    ).sessions[0].title == "人工标题"


async def test_metadata_failure_prevents_enqueue() -> None:
    catalog, queue = CatalogMessages(), _Queue()
    catalog.fail_ensure = True
    with pytest.raises(ServiceError):
        await service(catalog, queue=queue).submit_message(
            _CONTEXT,
            SubmitMessageCommand(
                message_id="m",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="hello"),),
            ),
        )
    assert queue.messages == []


async def test_generation_saves_title_and_failure_preserves_it() -> None:
    catalog = CatalogMessages()
    await catalog.ensure_session(_CONTEXT, title="原始", now=NOW)
    app = service(catalog)
    result = await app.generate_session_title(_CONTEXT)
    assert result.title == "生成标题" and result.title_version == 2
    before = await catalog.get_session(_CONTEXT)
    with pytest.raises(ServiceError):
        await service(catalog, TitleProvider("")).generate_session_title(_CONTEXT)
    assert await catalog.get_session(_CONTEXT) == before


async def test_rename_during_generation_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = CatalogMessages()
    await catalog.ensure_session(_CONTEXT, title="A", now=NOW)
    app = service(catalog)
    original = catalog.list_messages

    async def concurrent_read(ctx: SessionContext) -> tuple[Message, ...]:
        await catalog.update_session_title(ctx, title="B", now=NOW)
        await catalog.update_session_title(ctx, title="A", now=NOW)
        return await original(ctx)

    monkeypatch.setattr(catalog, "list_messages", concurrent_read)
    with pytest.raises(ServiceError) as error:
        await app.generate_session_title(_CONTEXT)
    assert error.value.code == "session_title_conflict"
    current = await catalog.get_session(_CONTEXT)
    assert current is not None and current.title == "A" and current.title_version == 3


async def test_missing_conversation_returns_not_found() -> None:
    app = service(CatalogMessages())
    for call in (
        app.update_session_title(_CONTEXT, "标题"),
        app.generate_session_title(_CONTEXT),
    ):
        with pytest.raises(ServiceError) as error:
            await call
        assert error.value.code == "session_not_found"


async def test_enqueue_failure_keeps_retryable_empty_conversation() -> None:
    class FailingQueue(_Queue):
        async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
            raise AdapterError(operation="inbox.push", message="入队失败")

    catalog = CatalogMessages()
    command = SubmitMessageCommand(
        message_id="m",
        role=InboundRole.USER,
        origin="user",
        content=(TextBlock(text="原始标题"),),
    )
    with pytest.raises(AdapterError):
        await service(catalog, queue=FailingQueue()).submit_message(_CONTEXT, command)
    info = await catalog.get_session(_CONTEXT)
    assert info is not None and info.title == "原始标题"
    await service(catalog).submit_message(_CONTEXT, command)
    assert await catalog.get_session(_CONTEXT) == info


async def test_catalog_and_manual_rename_work_without_registered_agent() -> None:
    catalog = CatalogMessages()
    await catalog.ensure_session(_CONTEXT, title="历史", now=NOW)
    app = ApplicationService(
        agents=AgentRegistry(()),
        inbox=_Queue(),
        states=_States(None),
        messages=catalog,
        clock=lambda: NOW,
    )
    assert (
        await app.list_sessions(SessionListQuery(app_id="app", user_id="user"))
    ).sessions
    assert (await app.update_session_title(_CONTEXT, "改名")).title == "改名"
    with pytest.raises(ServiceError) as error:
        await app.generate_session_title(_CONTEXT)
    assert error.value.code == "agent_not_found"


async def test_system_submission_does_not_leak_text_into_title() -> None:
    catalog = CatalogMessages()
    await service(catalog).submit_message(
        _CONTEXT,
        SubmitMessageCommand(
            message_id="system",
            role=InboundRole.SYSTEM,
            origin="system",
            content=(TextBlock(text="内部指令"),),
        ),
    )
    info = await catalog.get_session(_CONTEXT)
    assert info is not None and info.title == "新对话"
