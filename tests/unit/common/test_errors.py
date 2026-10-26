"""统一异常与结构化错误的行为测试。"""

import traceback

import pytest
from agentsystem.common import errors
from pydantic import ValidationError


def test_behavior_exceptions_use_safe_defaults() -> None:
    """各处理边界必须具有一致的默认 code 和安全消息。"""
    tool_error = errors.ToolError()
    adapter_error = errors.AdapterError(operation="state.read")
    service_error = errors.ServiceError(kind=errors.ServiceErrorKind.INVALID)
    run_failure = errors.RunFailure(operation="compaction")
    commit_error = errors.EventCommitError()

    assert tool_error.code == "tool_error"
    assert adapter_error.code == "adapter_error"
    assert adapter_error.operation == "state.read"
    assert service_error.code == "request_failed"
    assert run_failure.code == "execution_failed"
    assert run_failure.operation == "compaction"
    assert run_failure.notify_middleware is True
    assert commit_error.code == "event_commit_failed"
    assert commit_error.operation == "event.commit"


def test_custom_error_codes_are_validated() -> None:
    """调用点可自定义 code，但必须满足统一 snake_case 格式。"""
    error = errors.ServiceError(
        kind=errors.ServiceErrorKind.NOT_FOUND,
        code="agent_not_found",
        message="Agent 不存在",
    )
    assert error.code == "agent_not_found"

    with pytest.raises(ValueError):
        errors.RunFailure(operation="run", code="Invalid-Code")

    with pytest.raises(ValidationError):
        errors.LLMError(code="invalid.code", message="模型调用失败")


def test_run_failure_separates_safe_message_from_exception_text() -> None:
    """RunFailure 的公开消息和异常诊断正文必须彼此独立。"""
    failure = errors.RunFailure(
        operation="llm",
        code="provider_error",
        message="LLM provider 返回错误",
        exception_message="private provider detail",
    )

    assert failure.message == "LLM provider 返回错误"
    assert str(failure) == "private provider detail"


def test_format_run_failure_preserves_expected_failure_traceback() -> None:
    """预期失败仍须保留从具体失败点开始的 Runtime traceback。"""
    try:
        raise errors.RunFailure(
            operation="llm",
            code="provider_error",
            message="LLM provider 返回错误",
            exception_message="private provider detail",
        )
    except errors.RunFailure as failure:
        exception_type, stacktrace = errors.format_run_failure(failure)

    assert exception_type == "agentsystem.common.errors.RunFailure"
    assert "test_format_run_failure_preserves_expected_failure_traceback" in stacktrace
    assert "private provider detail" in stacktrace


def test_format_run_failure_uses_root_cause_and_preserves_long_stacktrace() -> None:
    """带 cause 的失败使用根异常类型，并完整保留超长堆栈。"""
    long_message = "provider-head-" + ("x" * 70_000) + "-provider-tail"
    try:
        try:
            raise ValueError(long_message)
        except ValueError as cause:
            raise errors.RunFailure(operation="compaction") from cause
    except errors.RunFailure as failure:
        exception_type, stacktrace = errors.format_run_failure(failure)

    assert exception_type == "ValueError"
    assert len(stacktrace) > 65_536
    assert long_message in stacktrace
    assert "stacktrace truncated" not in stacktrace
    assert stacktrace.endswith("\n")


def test_error_id_is_opaque_and_unique() -> None:
    """error_id 只提供不可猜测的日志关联能力。"""
    first = errors.new_error_id()
    second = errors.new_error_id()

    assert len(first) == 32
    assert first != second


def test_sanitized_exc_info_preserves_frames_and_cause_chain() -> None:
    """日志既能定位完整调用链，也不得保存第三方异常正文。"""
    inner_secret = "inner secret"
    outer_secret = "outer secret"
    try:
        try:
            raise ValueError(inner_secret)
        except ValueError as cause:
            raise RuntimeError(outer_secret) from cause
    except RuntimeError as error:
        rendered = "".join(
            traceback.format_exception(*errors.sanitized_exc_info(error))
        )

    assert "test_sanitized_exc_info_preserves_frames_and_cause_chain" in rendered
    assert "ValueError: message redacted" in rendered
    assert "RuntimeError: message redacted" in rendered
    assert "inner secret" not in rendered
    assert "outer secret" not in rendered
