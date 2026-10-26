"""真实应用装配、工具调用和跨 Agent/用户快照隔离。"""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from agentsystem.config.models import RuntimeConfig
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.message.models import TextBlock
from agentsystem.redis.client import close_redis_client, create_redis_client
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import SubmitMessageCommand
from litellm import ModelResponseStream
from tests.contract.redis.conftest import delete_prefix
from tests.integration.runtime.helpers import (
    _agent_config,
    _context,
    _wait_for_success,
)
from tests.redis_config import local_redis_config
from tests.runtime_config import TEST_ASSET_LIMITS, TEST_ASSET_STORE
from tests.unit.llm.stream_helpers import text_stream, tool_stream


@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="需要 TEST_REDIS_URL")
async def test_memory_tools_through_application(tmp_path: Path) -> None:
    prefix = "memory-e2e-" + uuid4().hex
    config = RuntimeConfig.model_validate(
        {
            "redis": local_redis_config(key_prefix=prefix),
            "asset": TEST_ASSET_LIMITS,
            "asset_store": TEST_ASSET_STORE,
            "memory": {"enabled": True, "store": {"type": "local"}},
            "dispatcher": {"ready_block_ms": 10},
        }
    )
    seen: list[str] = []
    step = 0
    edits = [
        ("memory_add", {"scope": "user_global", "new_content": "shared-old"}),
        ("memory_add", {"scope": "user_agent", "new_content": "private-only"}),
        (
            "memory_update",
            {"scope": "user_global", "old_content": "old", "new_content": "new"},
        ),
        ("memory_read", {"scope": "user_global"}),
    ]

    async def writer(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal step
        seen.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        if step >= len(edits):
            return text_stream("done")
        name, arguments = edits[step]
        step += 1
        return tool_stream(
            [
                {
                    "index": 0,
                    "id": f"call-{step}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
            response_id=f"response-{step}",
        )

    reader_prompts: list[str] = []

    async def reader(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        reader_prompts.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        return text_stream("done")

    redis = await create_redis_client(config.redis)
    service = await ServiceHost.create(
        runtime_config=config,
        project_root=tmp_path,
        consumer_id="memory-worker",
        redis_client=redis,
        agents=(
            AgentRuntimeSpec(
                agent_config=_agent_config("writer"),
                base_prompt="base",
                llm_provider=LiteLLMProvider(completion=writer),
            ),
            AgentRuntimeSpec(
                agent_config=_agent_config("reader"),
                base_prompt="base",
                llm_provider=LiteLLMProvider(completion=reader),
            ),
        ),
    )
    try:
        await service.start()
        for ctx in (
            _context("writer"),
            _context("reader"),
            _context("reader").model_copy(
                update={"user_id": "other", "session_id": "other-session"}
            ),
        ):
            await service.application.submit_message(
                ctx,
                SubmitMessageCommand(
                    message_id=uuid4().hex,
                    role=InboundRole.USER,
                    origin="user",
                    content=(TextBlock(text="go"),),
                ),
            )
            await _wait_for_success(service, ctx)
        assert step == 4
        assert "shared-new" in seen[-1]
        assert "<user_memory>" not in seen[-1]  # writer SYSTEM was saved before edits
        assert "shared-new" in reader_prompts[0]
        assert "private-only" not in reader_prompts[0]
        assert "shared-new" not in reader_prompts[1]
    finally:
        await service.close()
        await delete_prefix(redis, prefix)
        await close_redis_client(redis)
