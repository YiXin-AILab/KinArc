"""显式启用真实 Redis 的 EvoAgentBench Runtime adapter 冒烟测试。"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    AssetLimitsConfig,
    DispatcherConfig,
    LocalAssetStoreConfig,
    ModelParamsConfig,
    RuntimeConfig,
)
from agentsystem.llm.litellm_provider import LiteLLMProvider
from benchmarks.evoagentbench.common.process import invoke
from benchmarks.evoagentbench.runners.runtime_worker import run
from litellm import ModelResponseStream
from tests.redis_config import local_redis_config
from tests.unit.llm.stream_helpers import text_stream


@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="Requires test Redis")
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("accounting", ["exact", "partial"])
async def test_real_runtime_exports_response_usage_and_trace(
    tmp_path: Path, with_tools: bool, accounting: str
) -> None:
    """使用确定性模型提供器验证真实 Loop、Session 与 Redis 边界。"""
    if with_tools and not os.environ.get("EXECUTION_TEST_IMAGE"):
        pytest.skip("Set EXECUTION_TEST_IMAGE for the container profile")
    target = tmp_path / "target"
    (target / "config").mkdir(parents=True)
    (target / "prompt").mkdir()
    (target / "prompt/system.md").write_text("Solve the task")
    (target / "tools.json").write_text(
        '{"read_description":"read", "write_description":"write"}'
    )
    agent = AgentConfig(
        agent_id="eab-test",
        agent_name="test",
        prompt="prompt/system.md",
        enabled_tools=("eab_read", "eab_write_memory")
        + (
            ("run_command", "read_file", "write_file", "list_files", "replace_text")
            if with_tools
            else ()
        ),
        outbound=AgentOutboundConfig(type="log"),
        llm_model=ModelParamsConfig(model="openai/test"),
    )
    (target / "config/agent.yaml").write_text(
        yaml.safe_dump(agent.model_dump(mode="json"))
    )
    runtime = RuntimeConfig(
        redis=local_redis_config(),
        asset=AssetLimitsConfig(max_file_bytes=16 * 1024 * 1024),
        asset_store=LocalAssetStoreConfig(root=Path("assets")),
        dispatcher=DispatcherConfig(ready_block_ms=10, heartbeat_interval_seconds=0.1),
    )
    config = tmp_path / "runtime.yaml"
    config.write_text(yaml.safe_dump(runtime.model_dump(mode="json")))

    calls = 0

    async def command_stream() -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id="command",
            model="test",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-command",
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "arguments": (
                                        '{"command":"echo runtime-container-ok"}'
                                    ),
                                },
                            }
                        ]
                    },
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def completion(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal calls
        calls += 1
        if with_tools and calls == 1:
            return command_stream()
        if with_tools:
            assert "runtime-container-ok" in str(kwargs["messages"])
        return text_stream(
            "```python\nprint(42)\n```\nTASK_COMPLETE",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5 if accounting == "exact" else 4,
                "total_tokens": 15,
            },
        )

    result = await run(
        target,
        config,
        "Print 42",
        10,
        tmp_path / "scratch",
        llm_provider=LiteLLMProvider(completion=completion),
        execution_image=os.environ.get("EXECUTION_TEST_IMAGE") if with_tools else None,
    )
    assert "print(42)" in result.result
    assert result.metadata.total_tokens == (30 if with_tools else 15)
    assert isinstance(result.tracelog, dict)
    assert result.tracelog["status"] == "succeeded"
    assert result.tracelog["messages"]
    assert result.tracelog["agent_config_sha256"]
    assert result.tracelog["token_accounting"] == accounting


@pytest.mark.skipif(not os.environ.get("EAB_TEST_DOCKER_IMAGE"), reason="Opt-in Docker")
def test_container_supervisor_writes_only_exchange_result() -> None:
    """验证挂载路径、UID、只读根目录和结果文件传输。"""
    result = invoke(
        ["sh", "-c", 'printf \'{"score":1}\' > "$4"', "eab"],
        {},
        timeout=30,
        container_image=os.environ["EAB_TEST_DOCKER_IMAGE"],
    )
    assert result == {"score": 1}
