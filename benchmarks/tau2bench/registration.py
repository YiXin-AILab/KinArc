"""在原生 CLI 生成 Agent 选项前完成一次注册。"""

from typing import Any

from benchmarks.tau2bench.config import AGENT_NAME


def register_agentsystem_agent(registry: Any) -> None:
    """接收已初始化的上游 registry，避免循环导入。"""
    registry.register_agent_factory(_create_agent, AGENT_NAME)


def _create_agent(*args: Any, **kwargs: Any) -> Any:
    # 仅在执行时导入；提前导入 adapter 可能触发 Tau2 初始化。
    from benchmarks.tau2bench.adapter import create_agent

    return create_agent(*args, **kwargs)
