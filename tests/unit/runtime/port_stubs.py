"""Runtime 测试共用的完整领域端口替身。"""

from collections.abc import Sequence
from datetime import datetime

from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    ReadyEntry,
    ResumeReady,
    ResumeRequest,
)
from agentsystem.message.models import ContextMessage, Message
from agentsystem.message.sessions import SessionInfo, SessionListQuery, SessionPage
from agentsystem.runtime.state import AgentState
from agentsystem.state.manager import StateWriteResult


class UnexpectedPortCall(AssertionError):
    """测试调用了场景未声明的领域端口方法。"""


class MessageManagerStub:
    """默认拒绝全部调用的完整 MessageManager 测试基类。"""

    async def ensure_session(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        raise UnexpectedPortCall("ensure_session")

    async def delete_session(
        self, ctx: SessionContext, *, hard_delete: bool = False
    ) -> None:
        raise UnexpectedPortCall("delete_session")

    async def get_session(self, ctx: SessionContext) -> SessionInfo | None:
        raise UnexpectedPortCall("get_session")

    async def list_sessions(self, query: SessionListQuery) -> SessionPage:
        raise UnexpectedPortCall("list_sessions")

    async def update_session_title(
        self,
        ctx: SessionContext,
        *,
        title: str,
        now: datetime,
        expected_version: int | None = None,
    ) -> SessionInfo | None:
        raise UnexpectedPortCall("update_session_title")

    async def commit(
        self,
        ctx: SessionContext,
        messages: tuple[Message, ...],
    ) -> None:
        raise UnexpectedPortCall("commit")

    async def commit_compaction(
        self,
        ctx: SessionContext,
        message: Message,
        *,
        expected_latest_compaction_id: str | None,
    ) -> None:
        raise UnexpectedPortCall("commit_compaction")

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        raise UnexpectedPortCall("get")

    async def list_messages(
        self,
        ctx: SessionContext,
    ) -> tuple[Message, ...]:
        raise UnexpectedPortCall("list_messages")

    async def read_context_source(self, ctx: SessionContext) -> ContextMessage:
        raise UnexpectedPortCall("read_context_source")


class StateManagerStub:
    """默认拒绝全部调用的完整 StateManager 测试基类。"""

    async def get(self, ctx: SessionContext) -> AgentState | None:
        raise UnexpectedPortCall("get")

    async def create(self, initial_state: AgentState) -> StateWriteResult:
        raise UnexpectedPortCall("create")

    async def save(
        self,
        previous: AgentState,
        current: AgentState,
    ) -> StateWriteResult:
        raise UnexpectedPortCall("save")


class InboxStub:
    """默认拒绝全部调用的完整 Inbox 测试基类。"""

    async def push(
        self,
        ctx: SessionContext,
        message: InboundMessage,
    ) -> None:
        raise UnexpectedPortCall("push")

    async def push_resume(self, request: ResumeRequest) -> None:
        raise UnexpectedPortCall("push_resume")

    async def read_ready(
        self,
        consumer_id: str,
        *,
        agent_ids: Sequence[str],
        count: int,
        block_ms: int,
    ) -> tuple[ReadyEntry, ...]:
        raise UnexpectedPortCall("read_ready")

    async def requeue_resume(self, ready: ResumeReady) -> None:
        raise UnexpectedPortCall("requeue_resume")

    async def claim(
        self,
        ctx: SessionContext,
        modes: frozenset[InboundMode],
        *,
        owner_token: str,
    ) -> tuple[InboundMessage, ...]:
        raise UnexpectedPortCall("claim")

    async def complete(
        self,
        ctx: SessionContext,
        message_ids: tuple[str, ...],
        *,
        owner_token: str,
    ) -> None:
        raise UnexpectedPortCall("complete")

    async def list_pending(
        self,
        ctx: SessionContext,
    ) -> tuple[InboundMessage, ...]:
        raise UnexpectedPortCall("list_pending")
