"""Load evolvable native capabilities only in the public research container."""

import os
from pathlib import Path
from typing import Any

from agentsystem.config.models import AgentConfig, ModelParamsConfig
from agentsystem.plugin.discovery import discover_application_plugins
from agentsystem.plugin.models import PluginSource


def candidate_plugins(
    target: Path, config: AgentConfig, model_contract: dict[str, Any]
) -> tuple[PluginSource, ...]:
    """Check fixed execution conditions before executing any candidate Python."""
    if os.environ.get("EAB_CANDIDATE_ISOLATED") != "1":
        raise ValueError("Research candidate plugins require an isolated worker")
    expected = ModelParamsConfig.model_validate(model_contract)
    if config.llm_model != expected:
        raise ValueError("Research candidate changed the frozen model contract")
    return discover_application_plugins(target, config.agent_id)
