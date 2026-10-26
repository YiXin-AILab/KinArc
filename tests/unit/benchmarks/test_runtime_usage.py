"""用量统计问题不能导致已完成的评测输出被丢弃。"""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from agentsystem.llm.models import Usage, UsageAccounting
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import ServiceHost
from benchmarks.evoagentbench.runners.runtime_worker import execute


@pytest.mark.parametrize(
    "usage",
    [
        None,
        Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        Usage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            accounting=UsageAccounting.EXACT,
        ),
    ],
)
async def test_completed_output_survives_incomplete_usage(usage: Usage | None) -> None:
    snapshot = SimpleNamespace(
        status=SessionStatus.SUCCEEDED,
        usage=usage,
        result=SimpleNamespace(content="finished"),
        model_dump=lambda **kw: {
            "status": "succeeded",
            "usage": usage.model_dump(mode="json") if usage else None,
        },
    )
    history = SimpleNamespace(model_dump=lambda **kw: {"messages": []})
    app = SimpleNamespace(
        submit_message=AsyncMock(),
        snapshot=AsyncMock(return_value=snapshot),
        list_messages=AsyncMock(return_value=history),
    )
    result = await execute(
        cast(ServiceHost, SimpleNamespace(application=app)),
        "agent",
        "session",
        "prompt",
        0,
    )
    assert result.result == "finished"
    assert isinstance(result.tracelog, dict)
    assert result.tracelog["status"] == "succeeded"
    assert result.tracelog["token_accounting"] == (
        usage.accounting.value if usage else "missing"
    )
    assert result.metadata.total_tokens == (usage.total_tokens if usage else 0)


async def test_failed_session_is_not_promoted_to_success() -> None:
    snapshot = SimpleNamespace(
        status=SessionStatus.FAILED,
        usage=Usage(),
        result=None,
        model_dump=lambda **kw: {"status": "failed", "usage": None},
    )
    app = SimpleNamespace(
        submit_message=AsyncMock(),
        snapshot=AsyncMock(return_value=snapshot),
        list_messages=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda **kw: {"messages": []})
        ),
    )
    result = await execute(
        cast(ServiceHost, SimpleNamespace(application=app)),
        "agent",
        "session",
        "prompt",
        0,
    )
    assert isinstance(result.tracelog, dict)
    assert result.tracelog["status"] == "failed"


@pytest.mark.parametrize(
    "accounting, expected", [("exact", 15), ("partial", None), ("missing", None)]
)
def test_cost_summary_never_labels_partial_usage_exact(
    accounting: str, expected: int | None
) -> None:
    from benchmarks.evoagentbench.reporting.usage import summarize_tokens
    from evolution.models import CaseExecution, ExecutionMetadata, ExecutionResult

    case = CaseExecution(
        id="case",
        result="finished",
        tracelog={"token_accounting": accounting},
        metadata=ExecutionMetadata(total_tokens=15, duration_seconds=1),
    )
    summary = summarize_tokens(ExecutionResult(cases=(case,)))
    assert summary["tokens"] == expected
    assert summary["reported_tokens"] == 15
    assert summary["incomplete_token_trials"] == (accounting != "exact")
