"""Dispatcher 日志关联上下文测试。"""

import logging
from pathlib import Path
from typing import cast

from agentsystem.common.logging import setup_logging
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
    RuntimeLoggingConfig,
)
from agentsystem.inbox.models import NormalReady
from agentsystem.inbox.protocol import Inbox
from agentsystem.runtime.session import AgentSession, SessionExecutionKind
from agentsystem.service.agents import AgentRegistry, AgentRuntimeBundle
from agentsystem.service.dispatcher import TaskDispatcher


async def test_dispatcher_binds_ready_context_and_restores_after_task(
    tmp_path: Path,
) -> None:
    """Session 内部日志继承 Ready 标识，处理完成后不得污染调用方。"""
    logger = logging.getLogger("tests.dispatcher.context")

    class SessionStub:
        async def try_execute(self, ctx: SessionContext) -> object:
            assert ctx.session_id == "session-1"
            logger.info("inside dispatch")
            return SessionExecutionKind.EXECUTED

    agent_config = AgentConfig(
        agent_id="agent-1",
        agent_name="Agent",
        prompt="prompt",
        outbound=AgentOutboundConfig(type="log"),
        llm_model=ModelParamsConfig(model="openai/test"),
    )
    registry = AgentRegistry(
        (
            AgentRuntimeBundle(
                agent_config=agent_config,
                session=cast(AgentSession, SessionStub()),
            ),
        )
    )
    dispatcher = TaskDispatcher(
        inbox=cast(Inbox, object()),
        agents=registry,
        agent_ids=("agent-1",),
        consumer_id="worker-1",
        ready_block_ms=10,
        resume_requeue_delay_ms=0,
    )
    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )

    try:
        await dispatcher._handle_safely(
            NormalReady(
                app_id="app-1",
                user_id="user-1",
                agent_id="agent-1",
                session_id="session-1",
            )
        )
        logger.info("outside dispatch")
    finally:
        runtime.close()

    lines = (tmp_path / "logs" / "agentsystem.log").read_text().splitlines()
    inside, outside = lines
    assert "app=app-1 user=user-1 agent=agent-1 session=session-1" in inside
    assert "app=" not in outside
    assert "user=" not in outside
    assert "agent=" not in outside
    assert "session=" not in outside
