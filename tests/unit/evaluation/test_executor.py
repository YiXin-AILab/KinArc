"""evaluation.executor HTTP 执行器测试（MockTransport，不访问真实 Server）。"""

import asyncio
import logging
import re
from threading import Event

import httpx
import pytest

from evaluation.executor import HttpAgentExecutor
from evaluation.models import EvalCase


def _executor(handler: httpx.MockTransport, **overrides: object) -> HttpAgentExecutor:
    options = {
        "base_url": "http://testserver",
        "agent_id": "a1",
        "app_id": "evaluation",
        "user_id": "evaluation",
        "timeout_seconds": 30.0,
        "poll_interval_seconds": 0.01,
        "transport": handler,
    }
    options.update(overrides)
    return HttpAgentExecutor(**options)  # type: ignore[arg-type]


def _snapshot_payload(status: str) -> dict[str, object]:
    return {
        "session_id": "s1",
        "status": status,
        "result": {"content": "快照兜底回复"},
        "usage": {"total_tokens": 42},
        "timing": {"elapsed_ms": 1500.0},
    }


_MESSAGES_PAYLOAD = {
    "session_id": "s1",
    "messages": [
        {
            "message_id": "m1",
            "role": "user",
            "origin": "test",
            "content": "你好",
            "blocks": [{"type": "text", "text": "你好"}],
            "created_at": "2026-09-30T00:00:00Z",
        },
        {
            "message_id": "m2",
            "role": "assistant",
            "origin": "agent",
            "content": "",
            "blocks": [
                {
                    "type": "tool_call",
                    "tool_call_id": "t1",
                    "tool_name": "kb_search",
                    "arguments": {"query": "保养"},
                },
            ],
            "created_at": "2026-09-30T00:00:01Z",
        },
        {
            "message_id": "m3",
            "role": "tool",
            "origin": "agent",
            "content": "",
            "blocks": [
                {
                    "type": "tool_result",
                    "tool_call_id": "t1",
                    "tool_name": "kb_search",
                    "content": (
                        "## [pricing-001] A 保价格\n内容\n## [tech-002] 异响\n内容"
                    ),
                },
            ],
            "created_at": "2026-09-30T00:00:02Z",
        },
        {
            "message_id": "m4",
            "role": "assistant",
            "origin": "agent",
            "content": "",
            "blocks": [
                {"type": "text", "text": "A 保 1100-1400 元。"},
            ],
            "created_at": "2026-09-30T00:00:03Z",
        },
    ],
}


def test_execute_collects_reply_tools_docs_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=_MESSAGES_PAYLOAD)
        return httpx.Response(200, json=_snapshot_payload("succeeded"))

    executor = _executor(httpx.MockTransport(handler))
    outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    assert outcome.error is None
    assert outcome.reply == "A 保 1100-1400 元。"
    assert [call.name for call in outcome.tool_calls] == ["kb_search"]
    assert outcome.tool_calls[0].arguments == {"query": "保养"}
    assert outcome.retrieved_doc_ids == ["pricing-001", "tech-002"]
    assert outcome.tool_outputs == [
        "## [pricing-001] A 保价格\n内容\n## [tech-002] 异响\n内容"
    ]
    assert outcome.latency_seconds == 1.5
    assert outcome.total_tokens == 42


def test_execute_tolerates_snapshot_404_until_materialized() -> None:
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=_MESSAGES_PAYLOAD)
        state["polls"] += 1
        if state["polls"] < 3:
            return httpx.Response(404, json={"detail": "session_not_found"})
        return httpx.Response(200, json=_snapshot_payload("succeeded"))

    executor = _executor(httpx.MockTransport(handler))
    outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    assert outcome.error is None
    assert state["polls"] == 3


def test_execute_failed_session_surfaces_error() -> None:
    payload = _snapshot_payload("failed")
    payload["result"] = {"error_message": "模型调用失败"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=_MESSAGES_PAYLOAD)
        return httpx.Response(200, json=payload)

    executor = _executor(httpx.MockTransport(handler))
    outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    assert outcome.error == "模型调用失败"


def test_execute_times_out_when_never_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        return httpx.Response(200, json={"session_id": "s1", "status": "running"})

    executor = _executor(httpx.MockTransport(handler), timeout_seconds=0.05)
    outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    assert outcome.error is not None
    assert "TimeoutError" in outcome.error


def test_execute_submit_failure_becomes_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    executor = _executor(httpx.MockTransport(handler))
    outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    assert outcome.error is not None
    assert "500" in outcome.error


def test_execute_cancelled_via_stop_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        return httpx.Response(200, json={"session_id": "s1", "status": "running"})

    executor = _executor(httpx.MockTransport(handler), timeout_seconds=30.0)
    stop = Event()
    stop.set()
    # CancelledError 属于控制流，execute 不兜底、不记录，由调度器传播。
    with (
        caplog.at_level(logging.DEBUG, logger="evaluation.executor"),
        pytest.raises(asyncio.CancelledError),
    ):
        executor.execute(EvalCase(id="c1", input="你好"), stop)
    assert [r for r in caplog.records if r.name == "evaluation.executor"] == []


def test_execute_expected_http_error_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    executor = _executor(httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="evaluation.executor"):
        outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    assert outcome.error is not None
    assert "500" in outcome.error
    # 预期执行失败已写入报告 error 字段，不再记录日志堆栈。
    assert [r for r in caplog.records if r.name == "evaluation.executor"] == []


def test_execute_unexpected_error_logged_once_without_raw_message(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=_MESSAGES_PAYLOAD)
        return httpx.Response(200, json=_snapshot_payload("succeeded"))

    secret = "sk-test-secret-12345"

    def _boom(
        self: HttpAgentExecutor,
        case: EvalCase,
        snapshot: dict[str, object],
        messages: list[dict[str, object]],
        started: float,
    ) -> object:
        raise RuntimeError(f"api_key={secret}")

    monkeypatch.setattr(HttpAgentExecutor, "_build_outcome", _boom)
    executor = _executor(httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="evaluation.executor"):
        outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    # 单 Case 失败仍收敛为 error 字段，不终止整批，且不会被评为满分。
    assert outcome.error is not None
    # 未预期异常原文不得进入 outcome 或其序列化结果。
    assert secret not in outcome.error
    assert secret not in outcome.model_dump_json()
    # 未预期异常在最终所有权边界恰好记录一次完整脱敏堆栈。
    records = [r for r in caplog.records if r.name == "evaluation.executor"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert "evaluation_case_failed" in message
    assert "operation=evaluation.execute" in message
    assert "case_id=c1" in message
    assert record.exc_info is not None
    # 异常正文脱敏，原始敏感内容不得进入日志。
    assert secret not in caplog.text
    # outcome 只保存通用描述和 error_id，且与日志中的 error_id 一致。
    error_id_match = re.search(r"error_id=([0-9a-f]{32})", message)
    assert error_id_match is not None
    assert error_id_match.group(1) in outcome.error
    assert "RuntimeError" not in outcome.error


def test_execute_internal_value_error_is_unexpected(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=_MESSAGES_PAYLOAD)
        return httpx.Response(200, json=_snapshot_payload("succeeded"))

    def _boom(
        self: HttpAgentExecutor,
        case: EvalCase,
        snapshot: dict[str, object],
        messages: list[dict[str, object]],
        started: float,
    ) -> object:
        raise ValueError("内部实现缺陷，不应被静默")

    monkeypatch.setattr(HttpAgentExecutor, "_build_outcome", _boom)
    executor = _executor(httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="evaluation.executor"):
        outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    # 内部程序 ValueError 必须走未预期异常路径：记录一次堆栈，outcome 失败。
    assert outcome.error is not None
    assert "内部实现缺陷" not in outcome.error
    records = [
        r
        for r in caplog.records
        if r.name == "evaluation.executor" and r.levelno == logging.ERROR
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
    error_id_match = re.search(r"error_id=([0-9a-f]{32})", records[0].getMessage())
    assert error_id_match is not None
    assert error_id_match.group(1) in outcome.error


def test_execute_invalid_json_is_expected_format_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        return httpx.Response(200, content=b"not-json{")

    executor = _executor(httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="evaluation.executor"):
        outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    # 非法 JSON 是预期格式失败：outcome 失败但不记录堆栈。
    assert outcome.error is not None
    assert "ResponseFormatError" in outcome.error
    assert [r for r in caplog.records if r.name == "evaluation.executor"] == []


def test_execute_missing_messages_list_is_expected_format_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"session_id": "s1", "message_id": "m1"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"session_id": "s1", "messages": {"x": 1}})
        return httpx.Response(200, json=_snapshot_payload("succeeded"))

    executor = _executor(httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="evaluation.executor"):
        outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    # 缺少 messages 列表是预期格式失败：outcome 失败但不记录堆栈。
    assert outcome.error is not None
    assert "ResponseFormatError" in outcome.error
    assert [r for r in caplog.records if r.name == "evaluation.executor"] == []


def test_execute_http_status_error_strips_sensitive_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-test-secret-12345"
    url = f"http://testserver/v1/sessions/s1?api_key={secret}"
    request = httpx.Request("GET", url)
    response = httpx.Response(500, request=request)
    status_error = httpx.HTTPStatusError(
        f"Server error '500 Internal Server Error' for url '{url}'",
        request=request,
        response=response,
    )

    def _boom(
        self: HttpAgentExecutor,
        case: EvalCase,
        stop_event: Event,
        started: float,
    ) -> object:
        raise status_error

    monkeypatch.setattr(HttpAgentExecutor, "_execute", _boom)
    executor = _executor(httpx.MockTransport(lambda request: httpx.Response(500)))
    outcome = executor.execute(EvalCase(id="c1", input="你好"), Event())
    # httpx 错误消息带完整请求 URL，outcome 只保留状态码和安全分类。
    assert outcome.error is not None
    assert "500" in outcome.error
    assert secret not in outcome.error
    assert "api_key" not in outcome.error
    assert url not in outcome.error
