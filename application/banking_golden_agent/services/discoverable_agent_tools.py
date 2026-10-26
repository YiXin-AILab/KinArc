"""Banking Agent 只通过通用入口调用的隐藏工具。"""

from application.banking_golden_agent.services.discoverable_registry import (
    build_discoverable_registry,
)
from application.banking_golden_agent.services.tau_agent_discoverable_tools import (
    TauAgentDiscoverableTools,
)


class AgentDiscoverableTools(TauAgentDiscoverableTools):
    """使用 Tau 1.0.1 等价状态转换执行隐藏工具。"""


AGENT_DISCOVERABLE_REGISTRY = build_discoverable_registry(AgentDiscoverableTools)
