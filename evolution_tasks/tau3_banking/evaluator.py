"""Tau3 Banking 官方 RewardInfo 到通用 Evaluation 的映射。"""

import json
from collections.abc import Mapping
from pathlib import Path

from agentsystem.common.models import JsonValue
from evolution.models import (
    CaseEvaluation,
    CaseExecution,
    EvaluationResult,
    ExecutionResult,
)
from evolution.task import load_json_cases

from evolution_tasks.tau3_banking._internal.process import (
    DBChecks,
    EvaluationRequest,
    EvaluationResponse,
    RunnerError,
    Tau3BankingConfig,
    evaluate_tau2,
    load_banking_config,
)


def _response_feedback(response: EvaluationResponse) -> dict[str, JsonValue]:
    """提取单次官方评分的动作与数据库检查反馈。"""
    feedback: dict[str, JsonValue] = {}
    action_checks = response.reward_info.get("action_checks")
    if action_checks is not None:
        feedback["action_checks"] = action_checks
    if response.db_checks is not None:
        feedback["db_checks"] = _db_feedback(response.db_checks)
    return feedback


def _db_feedback(checks: DBChecks) -> JsonValue:
    """按数据库聚合差异，省略不存在的一侧以区别于 null。"""
    grouped: dict[str, dict[str, JsonValue]] = {}
    for difference in checks.differences:
        values: dict[str, JsonValue] = {}
        if difference.expected_present:
            values["expected"] = difference.expected
        if difference.actual_present:
            values["actual"] = difference.actual
        grouped.setdefault(difference.database, {})[difference.path] = values
    return {
        "db_match": checks.db_match,
        "db_reward": checks.db_reward,
        "differences": [
            {"database": database, "diff": differences}
            for database, differences in grouped.items()
        ],
    }


def _top_collection(path: str) -> str:
    """取 JSON Pointer 的顶层集合段，如 /credit_card_applications/data/<id>/field
    归一为 /credit_card_applications。同一集合不同记录、不同字段的失败因此
    可以跨 Case 聚类。
    """
    segments = [segment for segment in path.split("/") if segment]
    return f"/{segments[0]}" if segments else path


def _failure_kind(response: EvaluationResponse) -> str | None:
    """从官方评分结构提取最具代表性的 Case 不变失败类别，供规则层聚类。

    只取一个原子类别：首个失败动作的工具名，或首个 DB 差异的顶层集合。
    拼接完整失败清单会把签名退化为 Case 唯一，破坏跨 Case 聚类与问题库
    积累；完整失败明细仍在 feedback 中供 EvidenceTools 按需读取。
    """
    action_checks = response.reward_info.get("action_checks")
    if isinstance(action_checks, list):
        for check in action_checks:
            if not isinstance(check, dict) or check.get("action_match") is not False:
                continue
            action = check.get("action")
            name = action.get("name") if isinstance(action, dict) else None
            if isinstance(name, str) and name:
                return f"action_mismatch:{name}"
    checks = response.db_checks
    if checks is not None and not checks.db_match and checks.differences:
        first = checks.differences[0]
        return f"db_mismatch:{first.database}:{_top_collection(first.path)}"
    return None


class BankingEvaluator:
    """逐 Case 调用官方 Tau2 Evaluator 并输出安全确定性反馈。"""

    def __init__(self, config: Tau3BankingConfig) -> None:
        """保存官方评分所需的 Tau2 配置，不启动外部进程。"""
        self._config = config

    def evaluate(
        self,
        execution: ExecutionResult,
        dataset_path: Path,
    ) -> EvaluationResult:
        """调用官方 Tau2 评分器对已有 Simulation 评分。

        所有数据集共用评分规则；多 Trial 的分数取均值，诊断取最低分 Trial。
        评分会启动子进程，但不会再次运行 Candidate。

        Args:
            execution: 包含原始 Simulation 及 Candidate 事件的执行事实。
            dataset_path: 提供原始题目及官方评测条件的 JSON 文件。

        Returns:
            逐 Case 评分、诊断视图及 Case 分数的算术平均值。

        Raises:
            ValueError: 数据集为空、Case 集合不一致或评分证据格式无效。
            AdapterError: Tau2 评分子进程超时、失败或返回无效协议结果。
        """
        dataset = load_json_cases(dataset_path)
        if not dataset:
            raise ValueError("Tau3 Banking Dataset 不能为空")
        case_by_id = {case.id: case.content for case in dataset}
        evaluations = tuple(
            self._evaluate_case(case_by_id[case.id], case) for case in execution.cases
        )
        if set(case_by_id) != {case.id for case in execution.cases}:
            raise ValueError("Execution 与 Tau3 Banking Dataset Case ID 不一致")
        return EvaluationResult(
            total_score=sum(case.score for case in evaluations) / len(evaluations),
            cases=evaluations,
        )

    def _evaluate_case(
        self,
        case_content: dict[str, JsonValue],
        execution: CaseExecution,
    ) -> CaseEvaluation:
        """评分一次或多次采样，以最低分 Trial 生成代表性诊断视图。"""
        result = execution.result
        tracelog = execution.tracelog
        if execution.trials:
            responses = tuple(
                self._score_tracelog(case_content, execution.id, trial.tracelog)
                for trial in execution.trials
            )
            rewards = [_reward(response.reward_info) for response in responses]
            score = sum(rewards) / len(rewards)
            # 反馈附 reward 最低的那个 Trial 的检查明细，指向最需修复的证据。
            worst_index = rewards.index(min(rewards))
            worst = responses[worst_index]
            selected_trial = execution.trials[worst_index]
            result = selected_trial.result
            tracelog = selected_trial.tracelog
            feedback: dict[str, JsonValue] = {
                "trial_rewards": list(rewards),
                "representative_trial": worst_index + 1,
            }
            feedback.update(_response_feedback(worst))
            failure_kind = _failure_kind(worst)
        else:
            response = self._score_tracelog(
                case_content, execution.id, execution.tracelog
            )
            score = _reward(response.reward_info)
            feedback = _response_feedback(response)
            failure_kind = _failure_kind(response)
        assert isinstance(tracelog, dict)  # 已由 _score_tracelog 校验
        simulation = tracelog["simulation"]
        assert isinstance(simulation, dict)
        compact_tracelog: dict[str, JsonValue] = {
            "simulation": _compact_simulation(
                simulation, tracelog.get("candidate_events")
            )
        }
        raw_error = tracelog.get("error")
        if raw_error is not None:
            error = RunnerError.model_validate(raw_error)
            compact_tracelog["error"] = error.model_dump(mode="json", exclude_none=True)
            if failure_kind is None:
                failure_kind = f"runner_error:{error.code}"
        return CaseEvaluation(
            id=execution.id,
            result=result,
            tracelog=compact_tracelog,
            metadata=execution.metadata,
            expected=_safe_expected(case_content),
            score=score,
            feedback=(json.dumps(feedback, ensure_ascii=False) if feedback else ""),
            failure_kind=failure_kind,
        )

    def _score_tracelog(
        self,
        case_content: dict[str, JsonValue],
        case_id: str,
        tracelog: JsonValue,
    ) -> EvaluationResponse:
        """对单条 tracelog 的 simulation 调用官方 Tau2 Evaluator。"""
        if not isinstance(tracelog, dict):
            raise ValueError("Case tracelog 必须是 object")
        simulation = tracelog.get("simulation")
        if not isinstance(simulation, dict):
            raise ValueError("Case tracelog 缺少 simulation")
        response = evaluate_tau2(
            self._config,
            EvaluationRequest(
                case=case_content,
                retrieval_config=self._config.retrieval_config,
                simulation=simulation,
                judge_llm=self._config.judge_llm,
            ),
        )
        if response.case_id != case_id:
            raise ValueError("Tau2 EvaluationResponse Case ID 不一致")
        return response


def _candidate_messages(events: JsonValue | None) -> list[dict[str, JsonValue]]:
    """按事件顺序聚合待合入会话的模型输出和工具结果。"""
    if events is None:
        return []
    if not isinstance(events, list):
        raise ValueError("candidate_events 必须是 list")
    messages: list[dict[str, JsonValue]] = []
    results: list[dict[str, JsonValue]] = []
    current: dict[str, JsonValue] | None = None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("candidate_event 必须是 object")
        event_type = event.get("type")
        payload = event.get("payload")
        if event_type == "model.started":
            _finish_candidate_message(current, messages)
            messages.extend(results)
            results = []
            current = _start_candidate_message(payload)
        elif event_type == "message.block.completed":
            _append_candidate_content(current, payload)
        elif event_type == "tool.started":
            _append_candidate_tool_call(current, payload)
        elif event_type == "tool.completed":
            results.append(_candidate_tool_result(payload))
    _finish_candidate_message(current, messages)
    messages.extend(results)
    return messages


def _compact_simulation(
    simulation: dict[str, JsonValue], events: JsonValue | None = None
) -> dict[str, JsonValue]:
    """生成评测用会话视图，并合并模型输出；不修改原始评分会话。"""
    raw_messages = simulation.get("messages")
    if raw_messages is None:
        raw_messages = []
    if not isinstance(raw_messages, list):
        raise ValueError("simulation.messages 必须是 list")
    messages = [_compact_simulation_message(item) for item in raw_messages]
    _merge_candidate_messages(messages, _candidate_messages(events))
    return {"messages": list(messages)}


def _tool_message_positions(messages: list[dict[str, JsonValue]]) -> dict[str, int]:
    """索引 Assistant 工具调用所属消息，忽略用户侧工具调用。"""
    tool_positions: dict[str, int] = {}
    for index, message in enumerate(messages):
        calls = message.get("tool_calls")
        if message["role"] == "assistant" and isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict) and isinstance(call.get("id"), str):
                    tool_positions[str(call["id"])] = index
    return tool_positions


def _merge_candidate_messages(
    messages: list[dict[str, JsonValue]], candidates: list[dict[str, JsonValue]]
) -> None:
    """工具按 ID 对齐，回复按顺序匹配；未进入会话的输出按进度插入。"""
    tool_positions = _tool_message_positions(messages)
    unmatched: list[tuple[int, dict[str, JsonValue]]] = []
    # Tau2 的开场白不是 Candidate 输出，从第一个用户消息之后开始匹配。
    cursor = next(
        (i + 1 for i, message in enumerate(messages) if message["role"] == "user"),
        0,
    )
    for output in candidates:
        if output["role"] == "tool":
            continue
        calls = output.get("tool_calls")
        if isinstance(calls, list) and calls:
            positions = [
                tool_positions[str(call["id"])]
                for call in calls
                if isinstance(call, dict) and str(call["id"]) in tool_positions
            ]
            index = min(positions) if positions else None
            if positions:
                cursor = max(cursor, max(positions) + 1)
                assert index is not None
                _merge_missing_calls(messages[index], calls, tool_positions, index)
        else:
            index = _find_reply(messages, output.get("content"), cursor)
            if index is not None:
                cursor = index + 1
        if index is None:
            position = next(
                (
                    i
                    for i in range(cursor, len(messages))
                    if messages[i]["role"] in {"assistant", "user"}
                ),
                len(messages),
            )
            unmatched.append((position, output))
        elif "reasoning" in output:
            messages[index]["reasoning"] = output["reasoning"]
    for position, output in reversed(sorted(unmatched, key=lambda item: item[0])):
        messages.insert(position, output)
    _merge_missing_results(messages, candidates)


def _merge_missing_results(
    messages: list[dict[str, JsonValue]], candidates: list[dict[str, JsonValue]]
) -> None:
    """在调用消息落位后补结果，不复用回复匹配的游标。"""
    tool_positions = _tool_message_positions(messages)
    result_ids = _tool_result_ids(messages)
    unmatched: list[tuple[int, dict[str, JsonValue]]] = []
    for output in candidates:
        if output["role"] != "tool":
            continue
        call_id = str(output["id"])
        if call_id not in tool_positions or call_id in result_ids:
            continue
        position = next(
            (
                i
                for i in range(tool_positions[call_id] + 1, len(messages))
                if messages[i]["role"] in {"assistant", "user"}
            ),
            len(messages),
        )
        unmatched.append((position, output))
        result_ids.add(call_id)
    for position, output in reversed(sorted(unmatched, key=lambda item: item[0])):
        messages.insert(position, output)


def _merge_missing_calls(
    message: dict[str, JsonValue],
    calls: list[JsonValue],
    positions: dict[str, int],
    index: int,
) -> None:
    """按候选调用顺序补齐缺项，保留已有调用及其他消息中的调用。"""
    existing = message["tool_calls"]
    assert isinstance(existing, list)
    for offset, call in enumerate(calls):
        assert isinstance(call, dict)
        if str(call["id"]) in positions:
            continue
        following_ids = {
            str(item["id"]) for item in calls[offset + 1 :] if isinstance(item, dict)
        }
        insertion = next(
            (
                i
                for i, item in enumerate(existing)
                if isinstance(item, dict) and str(item["id"]) in following_ids
            ),
            len(existing),
        )
        existing.insert(insertion, call)
        positions[str(call["id"])] = index


def _tool_result_ids(messages: list[dict[str, JsonValue]]) -> set[str]:
    """收集已有工具结果，包含 Tau2 的并行工具消息。"""
    ids: set[str] = set()
    for message in messages:
        if message.get("role") == "tool" and isinstance(message.get("id"), str):
            ids.add(str(message["id"]))
        nested = message.get("tool_messages")
        if isinstance(nested, list):
            ids.update(
                _tool_result_ids([item for item in nested if isinstance(item, dict)])
            )
    return ids


def _candidate_tool_result(payload: JsonValue | None) -> dict[str, JsonValue]:
    """将 SSE 工具结果映射为会话消息，保留错误和截断标记。"""
    if not isinstance(payload, dict):
        raise ValueError("tool.completed payload 必须是 object")
    result: dict[str, JsonValue] = {
        "role": "tool",
        "id": _required_event_string(payload, "tool_call_id"),
        "requestor": "assistant",
        "content": payload.get("content"),
        "error": payload.get("is_error", False),
    }
    if payload.get("truncated"):
        result["truncated"] = True
    if payload.get("error_code") is not None:
        result["error_code"] = payload["error_code"]
    return result


def _find_reply(
    messages: list[dict[str, JsonValue]], text: JsonValue, cursor: int
) -> int | None:
    """从游标后匹配无工具调用的 Assistant 文本回复。"""
    if not isinstance(text, str) or not text:
        return None
    for index in range(cursor, len(messages)):
        message = messages[index]
        content = message.get("content")
        if (
            message["role"] == "assistant"
            and not message.get("tool_calls")
            and isinstance(content, str)
            and content.endswith(text)
        ):
            return index
    return None


def _compact_simulation_message(message: JsonValue) -> dict[str, JsonValue]:
    """提取会话证据字段，并递归保留并行工具消息。"""
    if not isinstance(message, dict):
        raise ValueError("simulation message 必须是 object")
    role = message.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("simulation message 缺少 role")
    compact: dict[str, JsonValue] = {"role": role}
    for field in ("id", "content", "requestor", "error"):
        value = message.get(field)
        if value is not None:
            compact[field] = value
    tool_calls = message.get("tool_calls")
    if tool_calls:
        if not isinstance(tool_calls, list):
            raise ValueError("simulation message.tool_calls 必须是 list")
        compact["tool_calls"] = [_compact_tool_call(item) for item in tool_calls]
    tool_messages = message.get("tool_messages")
    if tool_messages:
        if not isinstance(tool_messages, list):
            raise ValueError("simulation message.tool_messages 必须是 list")
        compact["tool_messages"] = [
            _compact_simulation_message(item) for item in tool_messages
        ]
    return compact


def _compact_tool_call(tool_call: JsonValue) -> dict[str, JsonValue]:
    """只保留诊断所需的工具身份、参数及请求方。"""
    if not isinstance(tool_call, dict):
        raise ValueError("simulation tool_call 必须是 object")
    return {
        field: tool_call[field]
        for field in ("id", "name", "arguments", "requestor")
        if field in tool_call and tool_call[field] is not None
    }


def _start_candidate_message(
    payload: JsonValue | None,
) -> dict[str, JsonValue]:
    """校验模型开始事件，并创建新的 Assistant 证据容器。"""
    if not isinstance(payload, dict):
        raise ValueError("model.started payload 必须是 object")
    return {"role": "assistant"}


def _append_candidate_content(
    current: dict[str, JsonValue] | None,
    payload: JsonValue | None,
) -> None:
    """按事件顺序累积文本或思考内容，忽略其他内容块。"""
    if not isinstance(payload, dict):
        raise ValueError("message.block.completed payload 必须是 object")
    block_type = payload.get("block_type")
    if block_type not in {"thinking", "text"}:
        return
    if current is None:
        raise ValueError("Candidate 输出缺少 model.started")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("Candidate 聚合内容必须是 string")
    field = "reasoning" if block_type == "thinking" else "content"
    previous = current.get(field, "")
    assert isinstance(previous, str)
    current[field] = previous + content


def _append_candidate_tool_call(
    current: dict[str, JsonValue] | None,
    payload: JsonValue | None,
) -> None:
    """记录工具调用证据，保留非法或截断参数以供诊断。"""
    if not isinstance(payload, dict):
        raise ValueError("tool.started payload 必须是 object")
    arguments = payload.get("arguments")
    if current is None:
        raise ValueError("Candidate 输出缺少 model.started")
    calls = current.setdefault("tool_calls", [])
    assert isinstance(calls, list)
    call: dict[str, JsonValue] = {
        "id": _required_event_string(payload, "tool_call_id"),
        "name": _required_event_string(payload, "tool_name"),
        "arguments": arguments,
        "requestor": "assistant",
    }
    # 此处只生成诊断证据，非法参数不得阻止已完成的官方评分落盘。
    if not isinstance(arguments, dict) or payload.get("truncated") is not False:
        call["raw_arguments"] = payload.get("raw_arguments")
        call["truncated"] = payload.get("truncated")
    calls.append(call)


def _finish_candidate_message(
    current: dict[str, JsonValue] | None,
    messages: list[dict[str, JsonValue]],
) -> None:
    """将包含实际输出的 Assistant 消息加入结果。"""
    if current is not None and len(current) > 1:
        messages.append(current)


def _required_event_string(payload: dict[str, JsonValue], field: str) -> str:
    """读取事件必需的非空字符串，缺失时拒绝构造证据。"""
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Candidate event 缺少 {field}")
    return value


def create_evaluator(
    task_dir: Path,
    settings: Mapping[str, JsonValue],
) -> BankingEvaluator:
    """Task Loader 固定 Factory。"""
    return BankingEvaluator(
        load_banking_config(task_dir, settings, entry_dir=Path(__file__).parent)
    )


def _reward(reward_info: dict[str, JsonValue]) -> float:
    """读取官方数值 reward，拒绝布尔值及非数字类型。"""
    value = reward_info.get("reward")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("RewardInfo.reward 必须是数字")
    return float(value)


def _safe_expected(case: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """从题目中提取评测条件和所需文档，省略其他任务字段。"""
    criteria = case.get("evaluation_criteria")
    expected = dict(criteria) if isinstance(criteria, dict) else {}
    required_documents = case.get("required_documents")
    if isinstance(required_documents, list):
        expected["required_documents"] = required_documents
    return expected


__all__ = ["BankingEvaluator", "create_evaluator"]
