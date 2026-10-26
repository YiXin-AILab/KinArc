"""V1 AgentState 纯状态转换测试。"""

from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.common.errors import RunError
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.llm.models import Usage
from agentsystem.runtime.state import (
    AgentState,
    SessionStatus,
    _replace_state,
    cancel_run,
    clear_context_token_checkpoint,
    complete_run,
    converge_orphaned_run,
    fail_run,
    resume_run,
    run_timing,
    set_context_token_checkpoint,
    start_run,
    start_turn,
    suspend_run,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 3, tzinfo=UTC)
LATER = NOW + timedelta(seconds=10)
CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def test_run_and_turn_follow_v1_happy_path() -> None:
    """新 run、steer 提升和成功终态按固定顺序转换。"""
    idle = AgentState(session_context=CONTEXT)

    running = start_run(
        idle,
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )
    assert running.status is SessionStatus.RUNNING
    assert running.turn_index == 0
    assert idle.status is SessionStatus.IDLE

    steered = running.model_copy(update={"steer_message_ids": ("m-2",)})
    turned = start_turn(steered)
    assert turned.turn_index == 1
    assert turned.current_message_ids == ("m-2",)
    assert turned.steer_message_ids == ()

    done = complete_run(turned, final_message_id="a-1", now=LATER)
    assert done.status is SessionStatus.SUCCEEDED
    assert done.final_message_id == "a-1"
    assert done.ended_at == LATER


def test_agent_state_rejects_attribute_assignment() -> None:
    """执行快照不能在状态转换函数之外原地修改。"""
    state = AgentState(session_context=CONTEXT)
    field_name = "status"

    with pytest.raises(ValidationError, match="frozen"):
        setattr(state, field_name, SessionStatus.RUNNING)


def test_state_replacement_revalidates_cross_field_contract() -> None:
    """状态替换不能绕过 AgentState 的终态不变量。"""
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )

    with pytest.raises(ValidationError, match="final_message_id"):
        _replace_state(
            running,
            status=SessionStatus.SUCCEEDED,
            ended_at=LATER,
            active_started_at=None,
            final_message_id=None,
        )


def test_terminal_state_cannot_transition_to_another_terminal_state() -> None:
    """已定稿的 run 终态不得被第二个终态覆盖。"""
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )
    done = complete_run(running, final_message_id="a-1", now=LATER)

    with pytest.raises(ValueError, match="RUNNING"):
        fail_run(
            done,
            error=RunError(code="late", message="late"),
            now=LATER,
        )


def test_new_run_resets_run_scoped_fields_and_preserves_output_mode() -> None:
    """终态之后的新 run 清空旧 run 数据，但保持会话输出模式。"""
    old_usage = Usage(input_tokens=8, output_tokens=5, total_tokens=13)
    previous = AgentState(
        session_context=CONTEXT,
        status=SessionStatus.FAILED,
        run_id="old-run",
        output_mode=OutputMode.COMPLETE,
        turn_index=3,
        current_message_ids=("old-current",),
        steer_message_ids=("old-steer",),
        usage_total=old_usage,
        final_message_id="old-final",
        started_at=NOW,
        ended_at=LATER,
        error=RunError(code="old", message="old"),
    )

    running = start_run(
        previous,
        run_id="new-run",
        output_mode=OutputMode.STREAM,
        now=LATER,
    )

    assert running.output_mode is OutputMode.COMPLETE
    assert running.turn_index == 0
    assert running.current_message_ids == ()
    assert running.steer_message_ids == ()
    assert running.usage_total == Usage()
    assert running.final_message_id is None
    assert running.ended_at is None
    assert running.error is None


def test_context_token_checkpoint_fields_are_all_or_none() -> None:
    """不完整的上下文 token 检查点不能进入持久化 State。"""
    with pytest.raises(ValidationError, match="context token checkpoint"):
        AgentState(session_context=CONTEXT, context_tokens=10)


def test_new_run_preserves_context_token_checkpoint() -> None:
    """上下文 token 检查点跨 run 保留。"""
    previous = AgentState(
        session_context=CONTEXT,
        context_tokens=50_000,
        context_token_checkpoint_message_id="assistant-old",
        context_token_request_fingerprint="fingerprint-old",
    )

    running = start_run(
        previous,
        run_id="run-new",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )

    assert running.context_tokens == 50_000
    assert running.context_token_checkpoint_message_id == "assistant-old"
    assert running.context_token_request_fingerprint == "fingerprint-old"


def test_context_token_checkpoint_transitions_preserve_usage() -> None:
    """设置或清除上下文检查点不得改变 run usage。"""
    usage = Usage(input_tokens=8, output_tokens=5, total_tokens=13)
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    ).model_copy(update={"usage_total": usage})

    checkpointed = set_context_token_checkpoint(
        running,
        tokens=50_000,
        message_id="assistant-1",
        request_fingerprint="fingerprint-1",
    )
    cleared = clear_context_token_checkpoint(checkpointed)

    assert checkpointed.context_tokens == 50_000
    assert checkpointed.context_token_checkpoint_message_id == "assistant-1"
    assert checkpointed.context_token_request_fingerprint == "fingerprint-1"
    assert checkpointed.usage_total == usage
    assert cleared.context_tokens is None
    assert cleared.context_token_checkpoint_message_id is None
    assert cleared.context_token_request_fingerprint is None
    assert cleared.usage_total == usage


def test_orphaned_running_state_converges_to_failed() -> None:
    """遗留 RUNNING 状态必须以稳定错误码收敛。"""
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )

    failed = converge_orphaned_run(running, now=LATER)

    assert failed.status is SessionStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "orphaned_run"


def test_pending_run_resumes_with_same_run_scope() -> None:
    """合法单次 token 恢复同一 run，并保留 turn、消息与 usage。"""
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    ).model_copy(
        update={
            "turn_index": 2,
            "current_message_ids": ("current",),
            "steer_message_ids": ("steer",),
            "usage_total": Usage(input_tokens=8, output_tokens=5, total_tokens=13),
        }
    )
    expires_at = LATER + timedelta(minutes=5)

    pending = suspend_run(
        running,
        tool_call_id="tool-call-1",
        resume_token="single-use-token",
        now=NOW,
        expires_at=expires_at,
    )
    assert pending.status is SessionStatus.PENDING
    assert pending.pending_tool_call_id == "tool-call-1"

    resumed = resume_run(
        pending,
        resume_token="single-use-token",
        now=LATER,
    )
    assert resumed.status is SessionStatus.RUNNING
    assert resumed.run_id == "run-1"
    assert resumed.turn_index == 2
    assert resumed.current_message_ids == ("current",)
    assert resumed.steer_message_ids == ("steer",)
    assert resumed.usage_total == running.usage_total
    assert resumed.pending_tool_call_id is None
    assert resumed.resume_token is None
    assert resumed.suspend_expires_at is None


def test_resume_rejects_wrong_or_expired_token() -> None:
    """错误或过期 token 不得消费 PENDING 状态。"""
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )
    pending = suspend_run(
        running,
        tool_call_id="tool-call-1",
        resume_token="single-use-token",
        now=NOW,
        expires_at=LATER,
    )

    with pytest.raises(ValueError, match="token"):
        resume_run(pending, resume_token="wrong-token", now=NOW)
    with pytest.raises(ValueError, match="过期"):
        resume_run(pending, resume_token="single-use-token", now=LATER)


def test_pending_run_can_fail_or_cancel_without_resume() -> None:
    """超时或取消可直接把 PENDING run 收敛到终态并清除凭证。"""
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )
    pending = suspend_run(
        running,
        tool_call_id="tool-call-1",
        resume_token="single-use-token",
        now=NOW,
    )
    error = RunError(
        code="timeout",
        message="timeout",
        error_id="opaque-error-id",
        exception_type="agentsystem.common.errors.RunFailure",
        stacktrace="Traceback ... private timeout detail\n",
    )

    failed = fail_run(pending, error=error, now=LATER)
    canceled = cancel_run(pending, error=error, now=LATER)

    assert failed.status is SessionStatus.FAILED
    assert failed.error == error
    assert failed.error.exception_type == "agentsystem.common.errors.RunFailure"
    assert failed.error.stacktrace == "Traceback ... private timeout detail\n"
    assert canceled.status is SessionStatus.CANCELED
    assert failed.resume_token is None
    assert canceled.resume_token is None


def test_run_timing_excludes_pending_wait_from_active_duration() -> None:
    """墙钟耗时包含挂起等待，active 耗时只累计 RUNNING 执行段。"""
    started_at = NOW
    suspended_at = NOW + timedelta(minutes=1)
    resumed_at = NOW + timedelta(minutes=11)
    ended_at = NOW + timedelta(minutes=12)
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.STREAM,
        now=started_at,
    )

    pending = suspend_run(
        running,
        tool_call_id="tool-call-1",
        resume_token="single-use-token",
        now=suspended_at,
    )
    resumed = resume_run(
        pending,
        resume_token="single-use-token",
        now=resumed_at,
    )
    done = complete_run(resumed, final_message_id="assistant-1", now=ended_at)

    assert done.active_started_at is None
    assert done.active_duration_ms == 120_000
    timing = run_timing(done)
    assert timing.elapsed_ms == 720_000
    assert timing.active_ms == 120_000


def test_pending_terminal_state_does_not_count_wait_as_active() -> None:
    """PENDING 直接失败或取消时不得把等待时间计入 active 耗时。"""
    suspended_at = NOW + timedelta(minutes=1)
    terminal_at = NOW + timedelta(minutes=11)
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.STREAM,
        now=NOW,
    )
    pending = suspend_run(
        running,
        tool_call_id="tool-call-1",
        resume_token="single-use-token",
        now=suspended_at,
    )
    error = RunError(code="timeout", message="timeout")

    failed = fail_run(pending, error=error, now=terminal_at)
    canceled = cancel_run(pending, error=error, now=terminal_at)

    assert failed.active_duration_ms == 60_000
    assert canceled.active_duration_ms == 60_000
    assert failed.active_started_at is None
    assert canceled.active_started_at is None


def test_active_timing_rejects_backwards_clock() -> None:
    """状态转换拒绝倒退时钟，避免产生伪造的负 active 耗时。"""
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.STREAM,
        now=NOW,
    )

    with pytest.raises(ValueError, match="时钟"):
        suspend_run(
            running,
            tool_call_id="tool-call-1",
            resume_token="single-use-token",
            now=NOW - timedelta(seconds=1),
        )
