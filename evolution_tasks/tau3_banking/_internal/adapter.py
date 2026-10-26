"""agentsystem HTTP/SSE 到 Tau2 半双工 Agent 的严格适配。"""

import importlib
import json
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Event, Thread
from typing import Any, cast
from uuid import uuid4

import httpx
from agentsystem.common.errors import AdapterError

from evolution_tasks.tau3_banking._internal.process import RunnerError
from evolution_tasks.tau3_banking._internal.tool_bridge import CaseToolBridge


def parse_candidate_events(
    events: tuple[dict[str, object], ...],
) -> str:
    """校验成功 run 的工具证据并返回聚合回复文本。"""
    builder = _TranscriptBuilder()
    for event in events:
        builder.add(event)
    return builder.finish()


@dataclass(slots=True)
class _TranscriptBuilder:
    """校验单次 run 的工具证据，并累积聚合文本与终态。"""

    completed_call_ids: set[str] = field(default_factory=set)
    seen_call_ids: set[str] = field(default_factory=set)
    text_parts: list[str] = field(default_factory=list)
    terminal: str | None = None
    terminal_error: RunnerError | None = None

    def add(self, event: dict[str, object]) -> None:
        """消费一个公开事件，校验工具配对并更新文本或终态。"""
        event_type = _required_string(event, "type")
        payload = _required_mapping(event, "payload")
        if (
            event_type == "message.block.completed"
            and payload.get("block_type") == "text"
        ):
            self.text_parts.append(_required_string(payload, "content"))
        elif event_type == "tool.started":
            self._add_call(_validate_tool_started(payload))
        elif event_type == "tool.completed":
            self._add_completion(_validate_tool_completed(payload))
        elif event_type == "tool.failed":
            raise ValueError("Candidate Tool 未完成")
        elif event_type in {"run.completed", "run.failed", "run.canceled"}:
            self.terminal = event_type
            self.terminal_error = _terminal_failure(event_type, payload)

    def finish(self) -> str:
        """确认成功终态与完整工具证据后返回文本，否则抛出对应错误。"""
        if self.terminal != "run.completed":
            if self.terminal_error is not None:
                raise AdapterError(
                    operation="tau3.candidate.run",
                    code=self.terminal_error.code,
                    message=self.terminal_error.message,
                )
            code = (
                "candidate_run_canceled"
                if self.terminal == "run.canceled"
                else "candidate_run_failed"
            )
            raise AdapterError(
                operation="tau3.candidate.run",
                code=code,
                message="Candidate run 未成功完成",
            )
        if self.seen_call_ids != self.completed_call_ids:
            raise ValueError("Candidate Tool 证据不完整")
        final_text = "".join(self.text_parts)
        if not self.seen_call_ids and not final_text.strip():
            raise ValueError("Candidate run 没有可重放输出")
        return final_text

    def _add_call(self, call_id: str) -> None:
        """登记尚未出现过的工具调用 ID，拒绝重复调用证据。"""
        if call_id in self.seen_call_ids:
            raise ValueError(f"重复 tool_call_id：{call_id}")
        self.seen_call_ids.add(call_id)

    def _add_completion(self, call_id: str) -> None:
        """登记对应已开始调用的唯一完成事件。"""
        if call_id not in self.seen_call_ids or call_id in self.completed_call_ids:
            raise ValueError(f"Tool completion 无法匹配：{call_id}")
        self.completed_call_ids.add(call_id)


class AgentsystemHttpSession:
    """同步提交用户 turn 并读取可恢复 SSE 的单 Case 客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        agent_id: str,
        user_id: str,
        session_id: str,
        timeout_seconds: float,
    ) -> None:
        """建立单 Case 的会话身份、截止时间与后台读取状态。"""
        self._base_url = base_url.rstrip("/")
        self._identity = {
            "app_id": app_id,
            "agent_id": agent_id,
            "user_id": user_id,
        }
        self._session_id = session_id
        self._timeout_seconds = timeout_seconds
        self._last_event_id: str | None = None
        self._last_completed_message_id: str | None = None
        self.candidate_events: list[dict[str, object]] = []
        self.failure: RunnerError | None = None
        self._deadline = time.monotonic() + timeout_seconds
        self._stopping = Event()
        self._closing = False
        self._active = Event()
        self._thread: Thread | None = None
        self._output: Future[str] | None = None

    def start(self, content: str) -> None:
        """后台读取一个用户回合，使主线程可以向 Tau2 交接工具。"""
        if self._stopping.is_set():
            raise ValueError("Candidate session 已关闭")
        if self._thread is not None and self._thread.is_alive():
            if self._output is None or not self._output.done():
                raise ValueError("上一 Candidate run 尚未完成")
            self._thread.join(timeout=1)
        output: Future[str] = Future()
        self._output = output

        def collect() -> None:
            """把后台 SSE 读取结果或异常传递给当前回合的 Future。"""
            try:
                output.set_result(self.submit(content))
            except Exception as error:
                output.set_exception(error)

        self._thread = Thread(target=collect, daemon=True, name="tau2-candidate-sse")
        self._thread.start()

    def next_output(self, bridge: CaseToolBridge) -> object:
        """等待下一个真实 Bridge 请求或 Candidate 最终回复。"""
        if self._output is None:
            raise ValueError("Candidate run 尚未启动")
        while time.monotonic() < self._deadline and not self._stopping.is_set():
            if self._output.done():
                return self._output.result()
            call = bridge.next_call()
            if call is not None:
                # 失败的 Candidate 不得继续执行它尚未交给 Tau2 的请求。
                if self._output.done():
                    return self._output.result()
                return call
        raise AdapterError(
            operation="tau3.candidate.wait",
            code="tau3_case_timeout",
            message="等待 Candidate 输出达到 Case 截止时间",
        )

    def close(self) -> None:
        """取消活跃 Candidate 并有限等待事件线程；可以重复调用。"""
        if self._stopping.is_set():
            return
        self._closing = True
        if self._active.is_set():
            try:
                with httpx.Client(timeout=1) as client:
                    response = client.post(
                        f"{self._base_url}/v1/sessions/{self._session_id}/cancel",
                        json=self._identity,
                    )
                    response.raise_for_status()
            except httpx.HTTPError as error:
                # CandidateServer 的进程关闭仍是最终所有权边界。
                self.candidate_events.append(
                    {
                        "type": "adapter.cleanup.failed",
                        "payload": {"exception_type": type(error).__name__},
                    }
                )
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def submit(self, content: str) -> str:
        """提交一个用户 turn，并消费到对应 run 终态。"""
        if not content.strip():
            raise ValueError("Tau2 UserMessage content 不能为空")
        deadline = min(self._deadline, time.monotonic() + self._timeout_seconds)
        message_id = uuid4().hex
        previous_message_id = self._last_completed_message_id
        self._last_completed_message_id = None
        self._active.set()
        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.post(
                f"{self._base_url}/v1/sessions/{self._session_id}/messages",
                json={
                    **self._identity,
                    "message_id": message_id,
                    "content": [{"type": "text", "text": content}],
                    "mode": "queue",
                },
            )
            response.raise_for_status()
            events = self._read_until_terminal(
                client, deadline, message_id, previous_message_id
            )
        aggregated_events = tuple(
            event for event in events if _is_replay_evidence(event)
        )
        if not self._closing:
            self.failure = _candidate_failure(aggregated_events)
        text = parse_candidate_events(aggregated_events)
        self._last_completed_message_id = message_id
        return text

    def _read_until_terminal(
        self,
        client: httpx.Client,
        deadline: float,
        message_id: str,
        previous_message_id: str | None,
    ) -> list[dict[str, object]]:
        """在 Case 预算内续订 SSE，收集本次提交的事件直到终态。"""
        events: list[dict[str, object]] = []
        boundary_cursor = self._last_event_id if previous_message_id else None
        awaiting_message_id: str | None = None
        while time.monotonic() < deadline and not self._stopping.is_set():
            headers = (
                {"Last-Event-ID": self._last_event_id}
                if self._last_event_id is not None
                else {}
            )
            remaining = max(0.01, deadline - time.monotonic())
            try:
                with client.stream(
                    "GET",
                    f"{self._base_url}/v1/sessions/{self._session_id}/events",
                    params=self._identity,
                    headers=headers,
                    # 默认心跳 15 秒，Redis 阻塞读取还可能增加 10 秒。
                    timeout=httpx.Timeout(remaining, read=min(30, remaining)),
                ) as response:
                    if _is_public_stream_pending(response):
                        time.sleep(min(0.05, remaining))
                        continue
                    if _has_stream_error(response, 400, "cursor_expired"):
                        if (
                            boundary_cursor is None
                            or self._last_event_id != boundary_cursor
                            or events
                        ):
                            raise _cursor_expired_error()
                        self._verify_message_boundary(
                            client, deadline, previous_message_id, message_id
                        )
                        self._last_event_id = None
                        boundary_cursor = None
                        awaiting_message_id = message_id
                        continue
                    response.raise_for_status()
                    self._consume_stream(response, events, awaiting_message_id)
                    if events and events[-1].get("type") in {
                        "run.completed",
                        "run.failed",
                        "run.canceled",
                    }:
                        return events
            except httpx.TransportError:
                continue
        raise AdapterError(
            operation="tau3.candidate.sse",
            code="tau3_case_timeout",
            message="等待 Candidate SSE 终态超时或已停止",
        )

    def _verify_message_boundary(
        self,
        client: httpx.Client,
        deadline: float,
        previous_message_id: str | None,
        message_id: str,
    ) -> None:
        """确认权威历史仍属于已完成回合或本次已提交输入。"""
        response = client.get(
            f"{self._base_url}/v1/sessions/{self._session_id}/messages",
            params=self._identity,
            timeout=max(0.01, min(30, deadline - time.monotonic())),
        )
        response.raise_for_status()
        snapshot = response.json()
        messages = snapshot.get("messages") if isinstance(snapshot, dict) else None
        if (
            not isinstance(messages, list)
            or snapshot.get("session_id") != self._session_id
        ):
            raise _cursor_expired_error()
        latest_user = next(
            (
                item
                for item in reversed(messages)
                if isinstance(item, dict) and item.get("role") == "user"
            ),
            None,
        )
        if latest_user is None or latest_user.get("message_id") not in {
            previous_message_id,
            message_id,
        }:
            raise _cursor_expired_error()

    def _consume_stream(
        self,
        response: httpx.Response,
        events: list[dict[str, object]],
        awaiting_message_id: str | None,
    ) -> None:
        """恢复时跳过旧回合；只有本次 message.started 之后才接收输出。"""
        for record in _iter_sse_records(response.iter_lines()):
            if self._stopping.is_set():
                break
            if record.id is not None and record.id == self._last_event_id:
                continue
            event = json.loads(record.data)
            if not isinstance(event, dict):
                raise ValueError("SSE data 必须是 JSON object")
            if record.id is not None:
                self._last_event_id = record.id
            if awaiting_message_id is not None and not events:
                if event.get("type") != "message.started":
                    continue
                message = _required_mapping(
                    _required_mapping(event, "payload"), "message"
                )
                if message.get("message_id") != awaiting_message_id:
                    continue
            events.append(event)
            self._record_event(event)
            if event.get("type") in {"run.completed", "run.failed", "run.canceled"}:
                break

    def _record_event(self, event: dict[str, object]) -> None:
        """即时保存证据，异常终态无需等完整 run 解析后才可见。"""
        if _is_replay_evidence(event):
            self.candidate_events.append(event)
        if event.get("type") in {"run.completed", "run.failed", "run.canceled"}:
            self._active.clear()
        if not self._closing and event.get("type") in {"run.failed", "run.canceled"}:
            self.failure = _candidate_failure((event,))


def _is_replay_evidence(event: dict[str, object]) -> bool:
    """仅保留 Tau3 重放和计分需要的聚合事件。"""
    event_type = event.get("type")
    if event_type == "message.block.completed":
        payload = event.get("payload")
        return isinstance(payload, Mapping) and payload.get("block_type") in {
            "text",
            "thinking",
        }
    return event_type in {
        "turn.started",
        "model.started",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "run.completed",
        "run.failed",
        "run.canceled",
    }


def _is_public_stream_pending(response: httpx.Response) -> bool:
    """判断异步提交后公开 Stream 尚未创建的短暂响应。"""
    expected_codes = {
        404: "session_not_found",
        409: "snapshot_required",
    }
    expected_code = expected_codes.get(response.status_code)
    if expected_code is None:
        return False
    return _has_stream_error(response, response.status_code, expected_code)


def _has_stream_error(response: httpx.Response, status: int, code: str) -> bool:
    """流式响应需要先读取 body 才能识别公开错误码。"""
    if response.status_code != status:
        return False
    try:
        response.read()
        payload = response.json()
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("code") == code


def _cursor_expired_error() -> AdapterError:
    """缺少可验证边界时保留错误码，禁止猜测或重放工具。"""
    return AdapterError(
        operation="tau3.candidate.sse",
        code="cursor_expired",
        message="Candidate SSE 游标过期，无法确认完整回合边界，停止恢复",
    )


def _optional_error_fields(error: Mapping[str, object]) -> dict[str, str]:
    """保留 Runtime 公开的完整诊断字段并拒绝畸形值。"""
    fields: dict[str, str] = {}
    for name in ("exception_type", "stacktrace", "error_id"):
        value = error.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ValueError(f"Candidate run error.{name} 必须是非空字符串")
        fields[name] = value
    return fields


def _terminal_failure(
    event_type: str,
    payload: dict[str, object],
) -> RunnerError | None:
    """把公开终态错误转换为 Evolution 自己的诊断模型。"""
    if event_type == "run.completed":
        return None
    error = payload.get("error")
    if event_type == "run.failed" and isinstance(error, Mapping):
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, str) and isinstance(message, str):
            return RunnerError(
                code=code,
                message=message,
                **_optional_error_fields(error),
            )
    code = (
        "candidate_run_canceled"
        if event_type == "run.canceled"
        else "candidate_run_failed"
    )
    return RunnerError(code=code, message="Candidate run 未成功完成")


def _candidate_failure(
    events: tuple[dict[str, object], ...],
) -> RunnerError | None:
    """从一次提交的聚合事件中提取终态诊断。"""
    for event in reversed(events):
        event_type = event.get("type")
        if event_type not in {"run.completed", "run.failed", "run.canceled"}:
            continue
        return _terminal_failure(
            event_type,
            _required_mapping(event, "payload"),
        )
    return None


@dataclass(slots=True)
class _SseRecord:
    """保存一条 SSE 记录的游标和聚合 data 文本。"""

    id: str | None = None
    data: str = ""


def _iter_sse_records(lines: Any) -> Any:
    """按空行组装 SSE 记录，合并多行 data 并保留事件 ID。"""
    current = _SseRecord()
    for line in lines:
        if not isinstance(line, str):
            raise ValueError("SSE 行必须是文本")
        if line == "":
            if current.data:
                yield current
            current = _SseRecord()
        elif line.startswith("id:"):
            current.id = line[3:].strip()
        elif line.startswith("data:"):
            value = line[5:].lstrip()
            current.data = f"{current.data}\n{value}" if current.data else value
    if current.data:
        yield current


def create_http_agent(
    *,
    tools: list[object],
    domain_policy: str,
    session: AgentsystemHttpSession,
    bridge: CaseToolBridge,
) -> object:
    """在 Tau2 环境中创建真正的 HalfDuplexAgent 子类实例。"""
    agent_module = importlib.import_module("tau2.agent.base_agent")
    message_module = importlib.import_module("tau2.data_model.message")
    half_duplex_agent = cast(type[Any], agent_module.HalfDuplexAgent)
    assistant_message = cast(type[Any], message_module.AssistantMessage)
    multi_tool_message = cast(type[Any], message_module.MultiToolMessage)
    tool_call = cast(type[Any], message_module.ToolCall)
    tool_message = cast(type[Any], message_module.ToolMessage)
    user_message = cast(type[Any], message_module.UserMessage)
    get_now = cast(
        Callable[[], str],
        importlib.import_module("tau2.utils.utils").get_now,
    )

    def initialize(instance: object) -> None:
        """将工具定义与官方领域策略传给 Tau2 半双工 Agent 基类。"""
        half_duplex_agent.__init__(
            instance,
            tools=tools,
            domain_policy=domain_policy,
        )

    def get_init_state(
        instance: object,
        message_history: list[object] | None = None,
    ) -> None:
        """返回无额外本地状态的初始值；会话状态由 HTTP Session 持有。"""
        del instance, message_history
        return None

    def generate_next_message(
        instance: object,
        message: object,
        state: object,
    ) -> tuple[object, object]:
        """交接用户输入或官方工具结果，再返回下一次调用或最终回复。"""
        del instance
        if isinstance(message, user_message):
            session.start(message.content or "")
        elif isinstance(message, (tool_message, multi_tool_message)):
            results = (
                message.tool_messages
                if isinstance(message, multi_tool_message)
                else [message]
            )
            for result in results:
                bridge.resolve(result)
        else:
            raise TypeError("Agent 只接受 Tau2 UserMessage 或 ToolMessage")
        output = session.next_output(bridge)
        if isinstance(output, str):
            if not output.strip():
                raise ValueError("Candidate run 没有最终回复")
            outgoing = assistant_message(role="assistant", content=output)
        else:
            identity = cast(Any, output)
            outgoing = assistant_message(
                role="assistant",
                content=None,
                tool_calls=[
                    tool_call(
                        id=identity.id,
                        name=identity.name,
                        arguments=identity.arguments,
                        requestor="assistant",
                    )
                ],
            )
        outgoing.timestamp = get_now()
        return outgoing, state

    def stop(instance: object, message: object = None, state: object = None) -> None:
        """停止接收工具调用并关闭 Candidate 会话。"""
        del instance, message, state
        bridge.close()
        session.close()

    agent_type = type(
        "AgentsystemBankingHttpAgent",
        (half_duplex_agent,),
        {
            "__init__": initialize,
            "get_init_state": get_init_state,
            "generate_next_message": generate_next_message,
            "stop": stop,
        },
    )
    return agent_type()


def _validate_tool_started(payload: dict[str, object]) -> str:
    """校验完整工具参数及身份，返回用于证据配对的调用 ID。"""
    if payload.get("truncated") is not False:
        raise ValueError("Tool arguments 被截断")
    arguments = payload.get("arguments")
    if arguments is None:
        raw_arguments = payload.get("raw_arguments")
        if not isinstance(raw_arguments, str):
            raise ValueError("Tool arguments 缺失")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise ValueError("Tool raw_arguments 不是合法 JSON") from error
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments 必须是 JSON object")
    call_id = _required_string(payload, "tool_call_id")
    _required_string(payload, "tool_name")
    return call_id


def _validate_tool_completed(payload: dict[str, object]) -> str:
    """校验未截断的工具结果和错误标志，返回调用 ID。"""
    if payload.get("truncated") is not False:
        raise ValueError("Tool result 被截断")
    content = payload.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("Tool result content 类型非法")
    is_error = payload.get("is_error")
    if not isinstance(is_error, bool):
        raise ValueError("Tool result is_error 缺失")
    return _required_string(payload, "tool_call_id")


def _required_string(mapping: dict[str, object], key: str) -> str:
    """读取非空字符串字段，缺失或类型错误时抛出 ValueError。"""
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"缺少字符串字段：{key}")
    return value


def _required_mapping(
    mapping: dict[str, object],
    key: str,
) -> dict[str, object]:
    """读取 object 字段，缺失或类型错误时抛出 ValueError。"""
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"缺少 object 字段：{key}")
    return value


__all__ = [
    "AgentsystemHttpSession",
    "create_http_agent",
    "parse_candidate_events",
]
