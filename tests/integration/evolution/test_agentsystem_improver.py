"""Evolution Agent 真实 ServiceHost/Redis 集成测试。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import evolution.agentsystem_improver as improver_module
import pytest
from agentsystem.common.errors import ServiceError
from agentsystem.config.models import DispatcherConfig, RuntimeConfig
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.redis.client import close_redis_client, create_redis_client
from evolution.agentsystem_improver import AgentSystemImprover
from evolution.diagnosis import build_diagnosis_brief
from evolution.improver import ImprovementRequest
from evolution.models import (
    CaseEvaluation,
    EvaluationResult,
    ExecutionMetadata,
)
from litellm import ModelResponseStream
from tests.contract.redis.conftest import delete_prefix
from tests.redis_config import local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)
from tests.unit.llm.stream_helpers import text_stream, tool_stream


@pytest.mark.parametrize("read_trace", [False, True])
@pytest.mark.parametrize("initial_failure", [None, "error", "timeout"])
async def test_improver_runs_inside_service_host_without_http(
    tmp_path: Path,
    read_trace: bool,
    monkeypatch: pytest.MonkeyPatch,
    initial_failure: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实 AgentLoop 应调用证据工具并把结果送回下一轮模型请求。"""
    caplog.set_level(logging.INFO, logger="evolution.agentsystem_improver")
    assets = tmp_path / "agent"
    (assets / "config").mkdir(parents=True)
    (assets / "prompt").mkdir()
    (assets / "config" / "agent.yaml").write_text(
        "agent_id: evolution-agent\n"
        "agent_name: Test improver\n"
        "prompt: prompt/system.md\n"
        "outbound: {type: log}\n"
        "llm_model:\n"
        "  model: ${EVOLUTION_LLM_MODEL}\n"
        "  api_key: ${EVOLUTION_LLM_API_KEY}\n"
        "  base_url: ${EVOLUTION_LLM_BASE_URL}\n",
        encoding="utf-8",
    )
    (assets / "prompt" / "system.md").write_text("Test improver.\n", encoding="utf-8")
    monkeypatch.setattr(improver_module, "__file__", str(tmp_path / "improver.py"))
    calls = 0
    failed_once = False
    provider_stopped = asyncio.Event()
    observed_tool_result: str | None = None
    observed_results: list[str] = []
    steps = (
        [
            ("read_case_evidence", {"case_id": "train-1"}),
            ("read_case_tracelog", {"case_id": "train-1", "limit": 1}),
            (
                "read_case_tracelog",
                {
                    "case_id": "train-1",
                    "limit": 1,
                    "truncate": False,
                },
            ),
            ("read_file", {"path": str(tmp_path / "changelog.md")}),
            (
                "write_file",
                {
                    "action": "append",
                    "path": str(tmp_path / "changelog.md"),
                    "new_content": "\nCHANGE_RESULT_candidate-B_one\n",
                },
            ),
            (
                "write_file",
                {
                    "action": "replace",
                    "path": str(tmp_path / "changelog.md"),
                    "old_content": "CHANGE_RESULT_candidate-B_one",
                    "new_content": "train-1: confirmed",
                    "replace_all": False,
                },
            ),
        ]
        if read_trace
        else []
    )

    async def completion(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal calls, observed_tool_result, failed_once
        if initial_failure and not failed_once:
            failed_once = True
            if initial_failure == "timeout":
                try:
                    await asyncio.Event().wait()
                finally:
                    provider_stopped.set()
            raise RuntimeError("isolated test provider failure")
        calls += 1
        if calls == 1:
            messages = cast(list[dict[str, object]], kwargs["messages"])
            user = [item for item in messages if item["role"] == "user"][-1]
            content = user["content"]
            if isinstance(content, list):
                prompt = "\n".join(
                    str(item.get("text", ""))
                    for item in cast(list[dict[str, object]], content)
                )
            else:
                assert isinstance(content, str)
                prompt = content
            assert "candidate-A" not in prompt
            assert "candidate-B" in prompt
            assert "train-1" in prompt
            assert "x" * 2001 not in prompt
            offered = cast(list[dict[str, object]], kwargs["tools"])
            assert all(
                cast(dict[str, object], item["function"])["name"]
                != "read_train_evaluation"
                for item in offered
            )
        if calls > 1:
            messages = cast(list[dict[str, object]], kwargs["messages"])
            tool_messages = [item for item in messages if item["role"] == "tool"]
            observed_tool_result = cast(str, tool_messages[-1]["content"])
            observed_results.append(observed_tool_result)
        if calls <= len(steps):
            name, arguments = steps[calls - 1]
            return tool_stream(
                [
                    {
                        "index": 0,
                        "id": f"read-evidence-call-{calls}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
                response_id="evolution-tool-response",
            )
        return text_stream("done", response_id="evolution-response")

    prefix = f"test:evolution:{uuid4().hex}"
    runtime = RuntimeConfig(
        redis=local_redis_config(key_prefix="unused"),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
    )
    redis = await create_redis_client(runtime.redis)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "changelog.md").write_text("A_HISTORY")
    metadata = ExecutionMetadata(total_tokens=1, duration_seconds=0.1)
    try:
        improver = await AgentSystemImprover.create(
            runtime_config=runtime,
            project_root=TEST_PROJECT_ROOT,
            redis_prefix=prefix,
            request_timeout_seconds=1 if initial_failure == "timeout" else 3,
            redis_client=redis,
            llm_provider=LiteLLMProvider(completion=completion),
            environ={
                "EVOLUTION_LLM_MODEL": "openai/test-model",
                "EVOLUTION_LLM_API_KEY": "test-key",
                "EVOLUTION_LLM_BASE_URL": "https://example.test/v1",
            },
        )
        async with improver:
            evaluation = EvaluationResult(
                total_score=0,
                cases=(
                    CaseEvaluation(
                        id="train-1",
                        result="failed",
                        tracelog="x" * 2001,
                        metadata=metadata,
                        expected={},
                        score=0,
                        feedback="failed",
                    ),
                ),
            )
            request = ImprovementRequest(
                objective="improve",
                base_candidate_id="candidate-B",
                changelog_source_candidate_id="candidate-B",
                evidence_candidate_id="candidate-B",
                output_candidate_id="candidate-output",
                evidence_parent_candidate_id="candidate-A",
                workspace_dir=workspace,
                read_only=(),
                train_evaluation=evaluation,
                diagnosis_brief=build_diagnosis_brief("candidate-B", evaluation),
                changelog_path=tmp_path / "changelog.md",
            )
            await _run_attempts(
                improver,
                request,
                tmp_path,
                initial_failure,
                provider_stopped,
            )
    finally:
        await delete_prefix(redis, prefix)
        await close_redis_client(redis)

    assert calls == len(steps) + 1
    if read_trace:
        assert observed_tool_result is not None
        evidence = json.loads(observed_results[0])
        assert evidence["score"] == 0
        assert evidence["expected"] == {}
        assert evidence["tracelog"]["total_lines"] == 1
        assert "x" * 2001 not in observed_results[0]
        assert observed_results[1] == (
            "--- train-1/tracelog total_lines=1\n1\t"
            + "x" * 2000
            + " [read_case_tracelog 展示截断：原文共 2001 字符；"
            "完整读取：offset=0, limit=1, truncate=false]\n--- next_offset=1 eof=true"
        )
        assert observed_results[2] == (
            "--- train-1/tracelog total_lines=1\n1\t"
            + "x" * 2001
            + "\n--- next_offset=1 eof=true"
        )
        assert "A_HISTORY" in observed_results[3]
        assert json.loads(observed_results[4])["action"] == "append"
        assert json.loads(observed_results[5])["replacements"] == 1
        assert (
            tmp_path / "changelog.md"
        ).read_text() == "A_HISTORY\ntrain-1: confirmed\n"
    else:
        assert observed_results == []
    sessions = [
        item.getMessage().split("session_id=", 1)[1].split(" ", 1)[0]
        for item in caplog.records
        if "improver_session_started" in item.getMessage()
    ]
    assert len(sessions) == (2 if initial_failure else 1)
    assert len(set(sessions)) == len(sessions)


async def _run_attempts(
    improver: AgentSystemImprover,
    request: ImprovementRequest,
    tmp_path: Path,
    initial_failure: str | None,
    provider_stopped: asyncio.Event,
) -> None:
    """验证真实终态或取消收敛后可在另一 Workspace 启动新会话。"""
    if initial_failure:
        expected = TimeoutError if initial_failure == "timeout" else ServiceError
        with pytest.raises(expected) as raised:
            await improver.improve(request)
        if isinstance(raised.value, ServiceError):
            assert raised.value.code == "improver_execution_failed"
        else:
            assert provider_stopped.is_set()
        second_workspace = tmp_path / "second-workspace"
        second_workspace.mkdir()
        request = request.model_copy(update={"workspace_dir": second_workspace})
    await improver.improve(request)
