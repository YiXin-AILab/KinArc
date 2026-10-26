"""Runtime 集成测试共用的最小 Agent 配置与终态等待工具。"""

import asyncio

from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
)
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import ServiceHost
from agentsystem.service.models import SessionSnapshot


def _agent_config(
    agent_id: str,
    *,
    output_mode: OutputMode = OutputMode.COMPLETE,
    outbound_type: str = "log",
) -> AgentConfig:
    """构造不读取部署文件的集成测试 Agent 配置。"""
    return AgentConfig(
        agent_id=agent_id,
        agent_name=f"{agent_id} name",
        prompt="unused.md",
        output_mode=output_mode,
        outbound=AgentOutboundConfig(type=outbound_type),
        llm_model=ModelParamsConfig(model="openai/test-model"),
    )


def _context(agent_id: str) -> SessionContext:
    """为指定 Agent 构造独立的测试会话。"""
    return SessionContext(
        app_id="app",
        user_id="user",
        agent_id=agent_id,
        session_id=f"session-{agent_id}",
    )


async def _wait_for_success(
    service: ServiceHost,
    ctx: SessionContext,
) -> SessionSnapshot:
    """轮询公开快照直至成功。"""
    async with asyncio.timeout(3):
        while True:
            snapshot = await service.application.snapshot(ctx)
            if snapshot is not None and snapshot.status is SessionStatus.SUCCEEDED:
                return snapshot
            await asyncio.sleep(0.01)
