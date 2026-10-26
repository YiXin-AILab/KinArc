"""使用真实 Runtime 与 Redis 验证 EAB Skill 发现及原生工具执行。"""

import json
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
from benchmarks.evoagentbench.runners.runtime_worker import run
from litellm import ModelResponseStream
from tests.redis_config import local_redis_config
from tests.unit.llm.stream_helpers import text_stream


@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="Requires test Redis")
async def test_eab_discovers_and_reads_native_skill(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / "config").mkdir(parents=True)
    (target / "prompt").mkdir()
    (target / "prompt/system.md").write_text("Complete the office delivery task.")
    (target / "tools.json").write_text(
        '{"read_description":"read", "write_description":"write"}'
    )
    root = target / "skills/delivery-check"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: delivery-check\ndescription: Check office deliverables\n---\n"
        "Read references/checks.md before delivering."
    )
    (root / "references/checks.md").write_text("Confirm required PDF and XLSX files.")
    (target / "skills.yaml").write_text(
        "skill_roots: [skills]\nenabled_skills: [delivery-check]\n"
    )
    agent = AgentConfig(
        agent_id="native-skill-test",
        agent_name="test",
        prompt="prompt/system.md",
        enabled_tools=("eab_read", "eab_write_memory"),
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
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(yaml.safe_dump(runtime.model_dump(mode="json")))
    calls = 0

    async def tool_stream(file_path: str) -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id=f"skill-{calls}",
            model="test",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"read-{calls}",
                                "type": "function",
                                "function": {
                                    "name": "skill_view",
                                    "arguments": json.dumps(
                                        {
                                            "name": "delivery-check",
                                            "file_path": file_path,
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def completion(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal calls
        calls += 1
        messages = str(kwargs["messages"])
        assert "Check office deliverables" in messages
        assert "skill_view" in str(kwargs["tools"])
        if calls == 1:
            # 仅注入目录元数据，读取正文必须发起真实工具调用。
            assert "Read references/checks.md before delivering." not in messages
            return tool_stream("SKILL.md")
        if calls == 2:
            assert "Read references/checks.md before delivering." in messages
            return tool_stream("references/checks.md")
        assert "Confirm required PDF and XLSX files." in messages
        return text_stream(
            "Checked deliverables.",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        )

    result = await run(
        target,
        runtime_path,
        "Prepare delivery",
        15,
        tmp_path / "scratch",
        llm_provider=LiteLLMProvider(completion=completion),
    )
    assert result.result == "Checked deliverables."
    assert calls == 3
    assert isinstance(result.tracelog, dict)
    assert result.tracelog["status"] == "succeeded"
    assert result.tracelog["enabled_skills"] == ["delivery-check"]
    assert (
        result.tracelog["agent_config_sha256"]
        != result.tracelog["effective_agent_config_sha256"]
    )
