"""一次隔离的答案生成属于当前试验，不得暗中多次重试择优。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agentsystem.config.models import ModelParamsConfig
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.models import UsageAccounting
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import ServiceHost
from agentsystem.service.models import (
    SessionMessagesView,
    SessionPublicResult,
    SessionRunTiming,
    SessionSnapshot,
    SessionTokenUsage,
)
from benchmarks.evoagentbench.domains.retrieval_finalizer import Finalization
from benchmarks.evoagentbench.runners.runtime_worker import execute
from tests.unit.llm.stream_helpers import text_stream


def provider_for(body: str) -> LiteLLMProvider:
    async def completion(**kwargs: Any) -> Any:
        return text_stream(
            body,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    return LiteLLMProvider(completion=completion)


MODEL = ModelParamsConfig(model="model", max_output_tokens=None)


def snapshot(
    answer: str | None, offset: int = 0, *, failed: bool = False
) -> SessionSnapshot:
    started = datetime(2026, 10, 8, tzinfo=UTC) + timedelta(seconds=offset)
    return SessionSnapshot(
        session_id="session",
        status=SessionStatus.FAILED if failed else SessionStatus.SUCCEEDED,
        result=SessionPublicResult(content=answer) if answer else None,
        usage=SessionTokenUsage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            accounting=UsageAccounting.EXACT,
        ),
        timing=SessionRunTiming(
            started_at=started,
            ended_at=started + timedelta(seconds=1),
            elapsed_ms=1000,
            active_ms=1000,
        ),
    )


def host_for(snapshots: list[SessionSnapshot]) -> tuple[ServiceHost, SimpleNamespace]:
    app = SimpleNamespace(
        submit_message=AsyncMock(),
        snapshot=AsyncMock(side_effect=snapshots),
        list_messages=AsyncMock(return_value=SessionMessagesView(session_id="session")),
    )
    return cast(ServiceHost, SimpleNamespace(application=app)), app


async def test_empty_answer_gets_one_closing_call_and_all_usage_is_counted() -> None:
    initial = snapshot(None)
    host, app = host_for([initial])
    result = await execute(
        host,
        "agent",
        "session",
        "question",
        0,
        finalization_model=MODEL,
        finalization_provider=provider_for(
            '{"explanation":"evidence","exact_answer":"answer","confidence":80}'
        ),
    )
    assert (
        result.result == "Explanation: evidence\nExact Answer: answer\nConfidence: 80%"
    )
    assert result.metadata.total_tokens == 30
    assert app.submit_message.await_count == 1
    assert isinstance(result.tracelog, dict)
    assert isinstance(result.tracelog["run_snapshots"], list)
    assert len(result.tracelog["run_snapshots"]) == 1
    assert result.tracelog["finalization_followups"] == 1
    finalization = Finalization.model_validate(result.tracelog["finalization"])
    assert finalization.response.usage.total_tokens == 15


async def test_second_empty_response_remains_empty_without_more_sampling() -> None:
    host, app = host_for([snapshot(None)])
    result = await execute(
        host,
        "agent",
        "session",
        "question",
        0,
        finalization_model=MODEL,
        finalization_provider=provider_for("\n"),
    )
    assert result.result == ""
    assert app.submit_message.await_count == 1
    assert result.metadata.total_tokens == 30
    assert isinstance(result.tracelog, dict)
    finalization = Finalization.model_validate(result.tracelog["finalization"])
    assert finalization.validation_error == "invalid_final_answer"


@pytest.mark.parametrize("answer,failed", [("answer", False), (None, True)])
async def test_valid_answer_or_failed_execution_never_gets_followup(
    answer: str | None, failed: bool
) -> None:
    host, app = host_for([snapshot(answer, failed=failed)])
    result = await execute(
        host,
        "agent",
        "session",
        "question",
        0,
        finalization_model=MODEL,
        finalization_provider=provider_for("must never be requested"),
    )
    assert app.submit_message.await_count == 1
    assert result.result == (answer or "")


async def test_other_domains_keep_existing_behavior() -> None:
    host, app = host_for([snapshot(None)])
    await execute(host, "agent", "session", "question", 0)
    assert app.submit_message.await_count == 1


async def test_closing_provider_failure_is_not_a_model_zero() -> None:
    async def failure(**kwargs: Any) -> Any:
        raise TimeoutError("upstream timeout")

    host, _ = host_for([snapshot(None)])
    result = await execute(
        host,
        "agent",
        "session",
        "question",
        0,
        finalization_model=MODEL,
        finalization_provider=LiteLLMProvider(completion=failure),
    )
    assert isinstance(result.tracelog, dict)
    assert result.tracelog["status"] == "failed"
    recorded = result.tracelog["snapshot"]
    assert isinstance(recorded, dict)
    assert recorded["status"] == "succeeded"
    closing = Finalization.model_validate(result.tracelog["finalization"])
    assert closing.response.error is not None
    assert closing.response.error.code == "timeout"


@pytest.mark.parametrize(
    "accounting", [UsageAccounting.TOTAL_ONLY, UsageAccounting.PARTIAL]
)
async def test_preserves_total_only_accounting(accounting: UsageAccounting) -> None:
    current = snapshot("answer").model_copy(
        update={"usage": SessionTokenUsage(total_tokens=15, accounting=accounting)}
    )
    host, _ = host_for([current])
    result = await execute(host, "agent", "session", "question", 0)
    assert isinstance(result.tracelog, dict)
    assert result.tracelog["token_accounting"] == accounting.value
