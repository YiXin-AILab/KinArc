"""StateProjector 的身份校验与 CAS 快照测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.runtime.events import AgentEventEnvelope, AgentEventFactory, RunStarted
from agentsystem.runtime.state import AgentState
from agentsystem.state.manager import StateWriteResult
from agentsystem.state.projector import StateProjector
from tests.unit.runtime.port_stubs import StateManagerStub

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


def _event() -> AgentEventEnvelope:
    return AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    ).create(RunStarted(initial_messages=()))


async def test_projector_saves_independent_state_snapshots() -> None:
    calls: list[tuple[AgentState, AgentState]] = []

    class Manager(StateManagerStub):
        async def save(
            self,
            previous: AgentState,
            current: AgentState,
        ) -> StateWriteResult:
            calls.append((previous, current))
            return StateWriteResult.APPLIED

    previous = AgentState(session_context=_CONTEXT)
    current = previous.model_copy(update={"turn_index": 1})

    await StateProjector(Manager()).project(
        _CONTEXT,
        _event(),
        previous,
        current,
    )

    assert calls == [(previous, current)]
    assert calls[0][0] is not previous
    assert calls[0][1] is not current


async def test_projector_rejects_state_from_another_session() -> None:
    other = _CONTEXT.model_copy(update={"session_id": "other"})
    previous = AgentState(session_context=other)

    with pytest.raises(ValueError, match="投影 State"):
        await StateProjector(StateManagerStub()).project(
            _CONTEXT,
            _event(),
            previous,
            previous,
        )
