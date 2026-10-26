"""HTTP 执行器：通过公开 V1 API 对每条 Case 运行独立 session 并收集事实。"""

import asyncio
import json
import logging
import time
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
from agentsystem.common.errors import ResponseFormatError
from agentsystem.common.logging import log_unexpected_exception

from evaluation.models import CaseOutcome, EvalCase, ToolCallRecord
from evaluation.rules import parse_retrieved_doc_ids

_LOGGER = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})

_TOOL_OUTPUT_MAX_CHARS = 2000


class HttpAgentExecutor:
    """以同步 HTTP 轮询方式执行评测 Case，适配线程池调度器。"""

    def __init__(
        self,
        *,
        base_url: str,
        agent_id: str,
        app_id: str,
        user_id: str,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """配置评测会话身份、HTTP 连接和轮询预算。

        Args:
            base_url: Runtime API 基础地址。
            agent_id: 被测 Agent 标识。
            app_id: 评测会话所属应用。
            user_id: 评测会话所属用户。
            timeout_seconds: 快照轮询的最长等待秒数，同时限制单次 HTTP 超时。
            poll_interval_seconds: 相邻轮询的等待秒数。
            transport: 可注入的 HTTP 传输实现，默认使用真实连接。

        Raises:
            ValueError: 超时或轮询间隔不是正数。
        """
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("timeout 与轮询间隔必须为正数")
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._app_id = app_id
        self._user_id = user_id
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._transport = transport

    def execute(self, case: EvalCase, stop_event: Event) -> CaseOutcome:
        """创建独立会话执行单条用例，并收集公开消息与运行结果。

        Args:
            case: 待执行的评测用例。
            stop_event: 轮询期间检查的停止信号。

        Returns:
            执行事实；预期和未预期失败均写入 error，未预期异常同时记录日志。

        Raises:
            asyncio.CancelledError: 收到停止信号或执行被取消。
        """
        started = time.monotonic()
        try:
            return self._execute(case, stop_event, started)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TimeoutError, ResponseFormatError) as error:
            # 预期失败（HTTP/超时/响应格式）：事实写入报告，不记堆栈。
            return _error_outcome(case, started, error)
        except Exception as error:  # noqa: BLE001 - 单 Case 失败不应终止整批
            # 未预期程序异常：最终所有权边界记录一次完整脱敏堆栈。
            error_id = log_unexpected_exception(
                _LOGGER,
                "evaluation_case_failed",
                error,
                operation="evaluation.execute",
                case_id=case.id,
            )
            return _unexpected_error_outcome(case, started, error_id=error_id)

    def _execute(
        self,
        case: EvalCase,
        stop_event: Event,
        started: float,
    ) -> CaseOutcome:
        session_id = f"eval-{uuid4().hex}"
        identity = {
            "app_id": self._app_id,
            "user_id": self._user_id,
            "agent_id": self._agent_id,
        }
        with httpx.Client(
            base_url=self._base_url,
            timeout=min(30.0, self._timeout_seconds),
            transport=self._transport,
        ) as client:
            response = client.post(
                f"/v1/sessions/{session_id}/messages",
                json={
                    **identity,
                    "message_id": uuid4().hex,
                    "content": [{"type": "text", "text": case.input}],
                },
            )
            response.raise_for_status()
            snapshot = self._poll_snapshot(client, session_id, identity, stop_event)
            messages = self._get_messages(client, session_id, identity)
        return self._build_outcome(case, snapshot, messages, started)

    def _poll_snapshot(
        self,
        client: httpx.Client,
        session_id: str,
        identity: dict[str, str],
        stop_event: Event,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            if stop_event.is_set():
                raise asyncio.CancelledError
            try:
                response = client.get(
                    f"/v1/sessions/{session_id}",
                    params=identity,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                # POST 接受后 session state 异步物化，期间的 404 视为未就绪。
                if error.response.status_code != 404:
                    raise
            else:
                snapshot = _parse_json_object(response)
                status = snapshot.get("status")
                if status in _TERMINAL_STATUSES or status == "pending":
                    return snapshot
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Case 执行超过 {self._timeout_seconds:.0f}s")
            if stop_event.wait(self._poll_interval_seconds):
                raise asyncio.CancelledError

    def _get_messages(
        self,
        client: httpx.Client,
        session_id: str,
        identity: dict[str, str],
    ) -> list[dict[str, Any]]:
        response = client.get(
            f"/v1/sessions/{session_id}/messages",
            params=identity,
        )
        response.raise_for_status()
        payload = _parse_json_object(response)
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            raise ResponseFormatError(message="消息响应缺少 messages 列表")
        return messages

    def _build_outcome(
        self,
        case: EvalCase,
        snapshot: dict[str, Any],
        messages: list[dict[str, Any]],
        started: float,
    ) -> CaseOutcome:
        status = snapshot.get("status")
        result = snapshot.get("result") or {}
        error: str | None = None
        if status == "pending":
            error = "会话进入 PENDING 等待，评测不支持人机交互续跑"
        elif status != "succeeded":
            error = str(result.get("error_message") or f"会话终态异常：{status}")
        reply_parts, tool_calls, doc_ids, tool_outputs = _collect_message_facts(
            messages
        )
        reply = "\n".join(reply_parts) or str(result.get("content") or "")
        timing = snapshot.get("timing") or {}
        elapsed_ms = timing.get("elapsed_ms")
        latency = (
            float(elapsed_ms) / 1000.0
            if isinstance(elapsed_ms, (int, float))
            else time.monotonic() - started
        )
        usage = snapshot.get("usage") or {}
        total_tokens = usage.get("total_tokens")
        return CaseOutcome(
            case_id=case.id,
            reply=reply,
            tool_calls=tool_calls,
            retrieved_doc_ids=doc_ids,
            tool_outputs=tool_outputs,
            latency_seconds=latency,
            total_tokens=(
                int(total_tokens)
                if isinstance(total_tokens, (int, float)) and total_tokens > 0
                else None
            ),
            error=error,
        )


def _parse_json_object(response: httpx.Response) -> dict[str, Any]:
    """在 JSON 解析边界把格式错误归一化为 ResponseFormatError。"""
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise ResponseFormatError(message="响应不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise ResponseFormatError(message="响应 JSON 不是对象")
    return payload


def _error_outcome(case: EvalCase, started: float, error: Exception) -> CaseOutcome:
    """把一次预期执行失败收敛为带安全错误事实的 CaseOutcome。"""
    return CaseOutcome(
        case_id=case.id,
        latency_seconds=time.monotonic() - started,
        error=_safe_error_text(error),
    )


def _unexpected_error_outcome(
    case: EvalCase,
    started: float,
    *,
    error_id: str,
) -> CaseOutcome:
    """未预期异常只保存通用描述和日志关联 ID，绝不保存异常正文。"""
    return CaseOutcome(
        case_id=case.id,
        latency_seconds=time.monotonic() - started,
        error=f"unexpected_error error_id={error_id}",
    )


def _safe_error_text(error: Exception) -> str:
    """生成可诊断但不包含 URL、query 或敏感正文的错误事实。"""
    if isinstance(error, httpx.HTTPStatusError):
        # httpx 错误消息包含完整请求 URL，可能带认证信息，只保留状态码。
        return f"HTTPStatusError status={error.response.status_code}"
    return f"{type(error).__name__}: {error}"


def _collect_message_facts(
    messages: list[dict[str, Any]],
) -> tuple[list[str], list[ToolCallRecord], list[str], list[str]]:
    """从公开消息快照提取回复文本、工具调用序列、命中 doc_id 与工具结果正文。"""
    reply_parts: list[str] = []
    tool_calls: list[ToolCallRecord] = []
    doc_ids: list[str] = []
    tool_outputs: list[str] = []
    for message in messages:
        role = message.get("role")
        blocks = message.get("blocks") or []
        for block in blocks:
            block_type = block.get("type")
            if role == "assistant" and block_type == "text" and block.get("text"):
                reply_parts.append(str(block["text"]))
            elif role == "assistant" and block_type == "tool_call":
                arguments = block.get("arguments")
                tool_calls.append(
                    ToolCallRecord(
                        name=str(block.get("tool_name") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
            elif block_type == "tool_result" and block.get("content"):
                content = str(block["content"])
                # 工具结果正文是 judge 忠实度的事实依据；截断防止超长。
                tool_outputs.append(content[:_TOOL_OUTPUT_MAX_CHARS])
                for doc_id in parse_retrieved_doc_ids(content):
                    if doc_id not in doc_ids:
                        doc_ids.append(doc_id)
    return reply_parts, tool_calls, doc_ids, tool_outputs


__all__ = ["HttpAgentExecutor"]
