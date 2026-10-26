"""Tau3 Banking 官方 RewardInfo 映射测试。"""

import json
from pathlib import Path

import pytest
from agentsystem.common.models import JsonValue
from evolution.agent.plugins.evidence_tools import EvidenceTools
from evolution.agentsystem_improver import _request_text
from evolution.diagnosis import build_diagnosis_brief
from evolution.improver import ImprovementRequest
from evolution.models import (
    CaseAttempt,
    CaseExecution,
    ExecutionMetadata,
    ExecutionResult,
)
from evolution_tasks.tau3_banking._internal.process import (
    DBChecks,
    DBDifference,
    EvaluationRequest,
    EvaluationResponse,
    Tau3BankingConfig,
)
from evolution_tasks.tau3_banking.evaluator import BankingEvaluator, _compact_simulation


@pytest.mark.parametrize(
    "reward_info",
    [
        {"reward": 1.0, "reward_breakdown": {"db": 1.0}},
        {"reward": 0.0, "reward_breakdown": {"db": 0.0}},
        {"reward": 0.0, "reward_breakdown": {"action": 0.0}},
        {"reward": 0.0, "reward_breakdown": {"communicate": 0.0}},
        {"reward": 0.0, "reward_breakdown": {"nl_assertions": 0.0}},
        {"reward": 0.0, "reward_breakdown": None, "info": {"note": "early"}},
        {"reward": 1.0, "action_checks": []},
        {"reward": 0.0, "action_checks": None},
    ],
)
def test_evaluator_maps_score_and_complete_expected_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reward_info: dict[str, object],
) -> None:
    """保留标准答案；动作检查为空数组时保留，缺失或 null 时反馈为空。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "id": "task_001",
                    "evaluation_criteria": {
                        "actions": [
                            {
                                "name": "apply_for_credit_card",
                                "arguments": {
                                    "card_type": "Silver Rewards Card",
                                    "annual_income": 180000,
                                },
                                "requestor": "user",
                                "action_id": "003_0",
                            }
                        ],
                        "communicate_info": [],
                        "reward_basis": ["DB"],
                    },
                    "required_documents": [
                        "doc_credit_cards_silver_rewards_card_001",
                        "doc_credit_cards_gold_rewards_card_001",
                    ],
                    "user_scenario": {"instructions": "do not expose"},
                    "initial_state": {"initialization_data": "do not expose"},
                    "user_tools": ["apply_for_credit_card"],
                }
            ]
        ),
        encoding="utf-8",
    )
    response = EvaluationResponse(case_id="task_001", reward_info=reward_info)
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        lambda config, request: response,
    )
    evaluator = BankingEvaluator(_config(tmp_path))
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="completed",
                tracelog={"simulation": {"id": "simulation-1", "messages": []}},
                metadata=ExecutionMetadata(total_tokens=1, duration_seconds=0.5),
            ),
        )
    )

    evaluation = evaluator.evaluate(execution, dataset)

    case = evaluation.cases[0]
    assert case.id == "task_001"
    assert case.score == reward_info["reward"]
    if reward_info.get("action_checks") is None:
        assert case.feedback == ""
    else:
        assert json.loads(case.feedback) == {
            "action_checks": reward_info["action_checks"]
        }
    assert case.expected == {
        "actions": [
            {
                "name": "apply_for_credit_card",
                "arguments": {
                    "card_type": "Silver Rewards Card",
                    "annual_income": 180000,
                },
                "requestor": "user",
                "action_id": "003_0",
            }
        ],
        "communicate_info": [],
        "reward_basis": ["DB"],
        "required_documents": [
            "doc_credit_cards_silver_rewards_card_001",
            "doc_credit_cards_gold_rewards_card_001",
        ],
    }


@pytest.mark.parametrize("action_match", [True, False])
@pytest.mark.parametrize("db_match", [None, True, False])
def test_action_checks_feedback_is_available_only_through_case_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_match: bool,
    db_match: bool | None,
) -> None:
    """完整动作检查按需返回，保留内层 JSON 写法且不注入优化 Prompt。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(json.dumps([{"id": "task_001"}]), encoding="utf-8")
    checks = [
        {
            "action": {
                "action_id": "001_0",
                "requestor": "assistant",
                "name": "call_discoverable_agent_tool",
                "arguments": {
                    "agent_tool_name": "apply_credit",
                    "arguments": '{"amount": 5.00, "reason": "退款"}',
                },
                "compare_args": None,
            },
            "action_match": action_match,
            "action_reward": float(action_match),
            "tool_type": "write",
        }
    ]
    db_checks = (
        None
        if db_match is None
        else DBChecks(
            db_match=db_match,
            db_reward=float(db_match),
            diagnosis_status="matched" if db_match else "differences_found",
            differences=[]
            if db_match
            else [
                DBDifference(database="agent", path="/balance", expected=100, actual=95)
            ],
        )
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        lambda config, request: EvaluationResponse(
            case_id="task_001",
            reward_info={"reward": 0.0, "action_checks": checks},
            db_checks=db_checks,
        ),
    )
    evaluation = BankingEvaluator(_config(tmp_path)).evaluate(
        ExecutionResult(
            cases=(
                CaseExecution(
                    id="task_001",
                    result="completed",
                    tracelog={"simulation": {"messages": []}},
                    metadata=ExecutionMetadata(total_tokens=1, duration_seconds=0.5),
                ),
            )
        ),
        dataset,
    )
    request = ImprovementRequest(
        objective="improve",
        base_candidate_id="candidate-1",
        changelog_source_candidate_id="candidate-1",
        evidence_candidate_id="candidate-1",
        output_candidate_id="candidate-output",
        evidence_parent_candidate_id=None,
        workspace_dir=tmp_path,
        read_only=(),
        train_evaluation=evaluation,
        diagnosis_brief=build_diagnosis_brief("candidate-1", evaluation),
        changelog_path=tmp_path / "changelog.md",
    )
    tools = EvidenceTools()
    tools.bind(request)
    evidence = json.loads(tools.read_case_evidence("task_001").content)

    expected_feedback: dict[str, object] = {"action_checks": checks}
    if db_checks is not None:
        assert db_match is not None
        expected_feedback["db_checks"] = {
            "db_match": db_match,
            "db_reward": float(db_match),
            "differences": []
            if db_match
            else [
                {
                    "database": "agent",
                    "diff": {"/balance": {"expected": 100, "actual": 95}},
                }
            ],
        }
    assert json.loads(evidence["feedback"]) == expected_feedback
    assert evidence["score"] == 0.0
    prompt = _request_text(request)
    assert '"action_checks":' not in prompt
    assert '"db_checks":' not in prompt
    assert "apply_credit" not in prompt


def test_db_feedback_groups_databases_and_preserves_missing_and_null() -> None:
    """省略缺失侧，但保留 null、数字类型和原始 JSON Pointer。"""
    from evolution_tasks.tau3_banking.evaluator import _db_feedback

    checks = DBChecks(
        db_match=False,
        db_reward=0,
        diagnosis_status="differences_found",
        differences=[
            DBDifference(
                database="agent", path="/a~1b", expected=None, actual_present=False
            ),
            DBDifference(
                database="agent", path="/extra", expected_present=False, actual=None
            ),
            DBDifference(database="user", path="/amount", expected=5, actual=5.0),
        ],
    )
    result = _db_feedback(checks)
    assert json.dumps(result) == json.dumps(
        {
            "db_match": False,
            "db_reward": 0.0,
            "differences": [
                {
                    "database": "agent",
                    "diff": {"/a~1b": {"expected": None}, "/extra": {"actual": None}},
                },
                {
                    "database": "user",
                    "diff": {"/amount": {"expected": 5, "actual": 5.0}},
                },
            ],
        }
    )
    assert checks.differences[0].actual_present is False


def test_failure_kind_extracts_case_invariant_categories() -> None:
    """失败类别取代表性原子类别：首个失败动作工具名或首个 DB 差异顶层集合。"""
    from evolution_tasks.tau3_banking.evaluator import _failure_kind

    response = EvaluationResponse(
        case_id="task_001",
        reward_info={
            "reward": 0.0,
            "action_checks": [
                {
                    "action": {"action_id": "002_0", "name": "get_account"},
                    "action_match": True,
                    "action_reward": 1.0,
                },
                {
                    "action": {
                        "action_id": "002_1",
                        "name": "apply_for_credit_card",
                        "arguments": {"annual_income": 250000},
                    },
                    "action_match": False,
                    "action_reward": 0.0,
                },
            ],
        },
        db_checks=DBChecks(
            db_match=False,
            db_reward=0.0,
            diagnosis_status="differences_found",
            differences=[
                DBDifference(
                    database="agent",
                    path="/credit_card_applications/data/0312c150d4ddf652",
                    expected={"annual_income": 250000},
                    actual=None,
                ),
            ],
        ),
    )
    # 动作失败优先于 DB 差异；Case 特有的记录 ID 不进入类别。
    assert _failure_kind(response) == "action_mismatch:apply_for_credit_card"

    db_only = EvaluationResponse(
        case_id="task_001",
        reward_info={"reward": 0.0},
        db_checks=DBChecks(
            db_match=False,
            db_reward=0.0,
            diagnosis_status="differences_found",
            differences=[
                DBDifference(
                    database="agent",
                    path="/credit_card_applications/data/0312c150d4ddf652/status",
                    expected="approved",
                    actual="pending",
                ),
            ],
        ),
    )
    assert _failure_kind(db_only) == "db_mismatch:agent:/credit_card_applications"

    passing = EvaluationResponse(
        case_id="task_001",
        reward_info={"reward": 1.0, "action_checks": []},
        db_checks=DBChecks(db_match=True, db_reward=1.0, diagnosis_status="matched"),
    )
    assert _failure_kind(passing) is None


def test_failure_kind_falls_back_to_runner_error_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """评分结构不含失败明细但 Runtime 报错时，类别取 runner_error:<code>。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(json.dumps([{"id": "task_001"}]), encoding="utf-8")
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        lambda config, request: EvaluationResponse(
            case_id="task_001", reward_info={"reward": 0.0}
        ),
    )
    evaluation = BankingEvaluator(_config(tmp_path)).evaluate(
        ExecutionResult(
            cases=(
                CaseExecution(
                    id="task_001",
                    result="failed",
                    tracelog={
                        "simulation": {"messages": []},
                        "error": {"code": "tool_timeout", "message": "boom"},
                    },
                    metadata=ExecutionMetadata(total_tokens=1, duration_seconds=0.5),
                ),
            )
        ),
        dataset,
    )
    assert evaluation.cases[0].failure_kind == "runner_error:tool_timeout"


def test_db_feedback_unavailable_is_not_reported_as_matched() -> None:
    """没有诊断证据不能当成 DB 匹配成功。"""
    from evolution_tasks.tau3_banking.evaluator import _db_feedback

    assert _db_feedback(
        DBChecks(db_match=False, db_reward=0, diagnosis_status="unavailable")
    ) == {"db_match": False, "db_reward": 0.0, "differences": []}


def test_evaluator_keeps_compact_simulation_messages_in_tracelog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evaluation 只保留 Simulation 消息中的业务字段。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(
        json.dumps([{"id": "task_001", "evaluation_criteria": {}}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        lambda config, request: EvaluationResponse(
            case_id="task_001",
            reward_info={"reward": 1.0},
        ),
    )
    tool_content = "完整工具正文" * 5_000
    simulation = {
        "id": "simulation-1",
        "termination_reason": "agent_stop",
        "duration": 10.5,
        "start_time": "drop-me",
        "messages": [
            {
                "role": "assistant",
                "content": "我来查询。",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "lookup",
                        "arguments": {"user_id": "u-1"},
                        "requestor": "assistant",
                    }
                ],
                "usage": {"total_tokens": 99},
                "raw_data": {"provider": "drop-me"},
                "timestamp": "drop-me",
            },
            {
                "role": "tool",
                "id": "call-1",
                "content": tool_content,
                "error": False,
                "requestor": "assistant",
                "timestamp": "drop-me",
                "turn_idx": 1,
            },
            {
                "role": "user",
                "content": "谢谢。",
                "tool_calls": [],
                "audio_path": "drop-me",
                "cost": 1.5,
            },
        ],
    }
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="completed",
                tracelog={
                    "candidate_events": [{"type": "drop-me"}],
                    "simulation": simulation,
                },
                metadata=ExecutionMetadata(
                    total_tokens=12_345,
                    duration_seconds=67.5,
                ),
            ),
        )
    )

    case = (
        BankingEvaluator(_config(tmp_path))
        .evaluate(
            execution,
            dataset,
        )
        .cases[0]
    )

    assert case.tracelog == {
        "simulation": {
            "messages": [
                {
                    "role": "assistant",
                    "content": "我来查询。",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "lookup",
                            "arguments": {"user_id": "u-1"},
                            "requestor": "assistant",
                        }
                    ],
                },
                {
                    "role": "tool",
                    "id": "call-1",
                    "content": tool_content,
                    "error": False,
                    "requestor": "assistant",
                },
                {"role": "user", "content": "谢谢。"},
            ]
        }
    }
    assert case.metadata.total_tokens == 12_345
    assert case.metadata.duration_seconds == 67.5


def test_evaluator_merges_reasoning_into_simulation_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """思考归属首个工具调用或回复，评分输入及原始执行记录不变。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(
        json.dumps([{"id": "task_001", "evaluation_criteria": {}}]),
        encoding="utf-8",
    )
    captured: list[EvaluationRequest] = []

    def official_evaluation(
        config: Tau3BankingConfig, request: EvaluationRequest
    ) -> EvaluationResponse:
        del config
        captured.append(request)
        return EvaluationResponse(case_id="task_001", reward_info={"reward": 1.0})

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2", official_evaluation
    )
    candidate_events = [
        {
            "type": "model.started",
            "payload": {"provider_name": "banking", "model": "test-model"},
        },
        {
            "type": "message.block.completed",
            "payload": {"block_type": "thinking", "content": "先验证身份。"},
        },
        {
            "type": "message.block.completed",
            "payload": {"block_type": "text", "content": "请提供姓名。"},
        },
        {
            "type": "tool.started",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "lookup_user",
                "arguments": {"name": "Alice"},
                "truncated": False,
            },
        },
        {
            "type": "tool.started",
            "payload": {
                "tool_call_id": "call-2",
                "tool_name": "lookup_account",
                "arguments": {},
                "truncated": False,
            },
        },
        {
            "type": "model.started",
            "payload": {"provider_name": "banking", "model": "test-model"},
        },
        {
            "type": "message.block.completed",
            "payload": {"block_type": "thinking", "content": "返回查询结果。"},
        },
        {
            "type": "message.block.delta",
            "payload": {"block_type": "text", "delta": "不应落盘"},
        },
        {
            "type": "message.block.completed",
            "payload": {"block_type": "text", "content": "查询完成。"},
        },
    ]
    messages: list[dict[str, JsonValue]] = [
        {"role": "assistant", "content": "欢迎。"},
        {"role": "user", "content": "查询账户。"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "lookup_user",
                    "arguments": {"name": "Alice"},
                    "requestor": "assistant",
                }
            ],
        },
        {"role": "tool", "id": "call-1", "content": "用户信息"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-2",
                    "name": "lookup_account",
                    "arguments": {},
                    "requestor": "assistant",
                }
            ],
        },
        {"role": "tool", "id": "call-2", "content": "账户信息"},
        {"role": "assistant", "content": "请提供姓名。查询完成。"},
    ]
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="completed",
                tracelog={
                    "simulation": {
                        "termination_reason": "agent_stop",
                        "messages": messages,
                    },
                    "candidate_events": candidate_events,
                },
                metadata=ExecutionMetadata(total_tokens=10, duration_seconds=1),
            ),
        )
    )

    original_execution = execution.model_dump_json()
    case = (
        BankingEvaluator(_config(tmp_path))
        .evaluate(
            execution,
            dataset,
        )
        .cases[0]
    )

    tracelog = case.tracelog
    assert isinstance(tracelog, dict)
    assert tracelog == {
        "simulation": {
            "messages": [
                *messages[:2],
                {**messages[2], "reasoning": "先验证身份。"},
                *messages[3:6],
                {**messages[6], "reasoning": "返回查询结果。"},
            ]
        }
    }
    assert execution.model_dump_json() == original_execution
    assert captured[0].simulation == {
        "termination_reason": "agent_stop",
        "messages": messages,
    }


def test_reasoning_matches_repeated_replies_in_order() -> None:
    """相同回复不应匹配开场白或覆盖前一回合的思考。"""
    messages: list[JsonValue] = [
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "好的"},
    ]
    events: list[JsonValue] = []
    for reasoning in ("第一问思考", "第二问思考"):
        events.extend(
            [
                {"type": "model.started", "payload": {}},
                {
                    "type": "message.block.completed",
                    "payload": {"block_type": "thinking", "content": reasoning},
                },
                {
                    "type": "message.block.completed",
                    "payload": {"block_type": "text", "content": "好的"},
                },
                {"type": "run.completed", "payload": {}},
            ]
        )
    assert _compact_simulation({"messages": messages}, events) == {
        "messages": [
            *messages[:2],
            {"role": "assistant", "content": "好的", "reasoning": "第一问思考"},
            messages[3],
            {"role": "assistant", "content": "好的", "reasoning": "第二问思考"},
        ]
    }


def test_interrupted_reasoning_is_preserved_without_attaching_to_old_reply() -> None:
    """尚未进入 Tau2 会话的部分输出在评测视图末尾保留。"""
    messages: list[JsonValue] = [
        {"role": "assistant", "content": "先前回复"},
        {"role": "user", "content": "继续"},
    ]
    events: list[JsonValue] = [
        {"type": "model.started", "payload": {}},
        {
            "type": "message.block.completed",
            "payload": {"block_type": "thinking", "content": "先检查。"},
        },
        {
            "type": "message.block.completed",
            "payload": {"block_type": "thinking", "content": "再判断。"},
        },
        {"type": "run.failed", "payload": {}},
    ]
    assert _compact_simulation({"messages": messages}, events) == {
        "messages": [
            *messages,
            {"role": "assistant", "reasoning": "先检查。再判断。"},
        ]
    }


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("is_error", [False, True])
def test_candidate_tool_results_are_merged_with_reasoning(
    existing: bool, is_error: bool
) -> None:
    """本地工具结果随思考补入，已有结果保持原样且不重复。"""
    call: JsonValue = {
        "id": "local-1",
        "name": "skill_view",
        "arguments": {},
        "requestor": "assistant",
    }
    result: JsonValue = {
        "role": "tool",
        "id": "local-1",
        "content": "skill result",
        "requestor": "assistant",
        "error": is_error,
    }
    assistant: JsonValue = {"role": "assistant", "tool_calls": [call]}
    messages: list[JsonValue] = [{"role": "user", "content": "help"}]
    if existing:
        messages.extend([assistant, result])
    messages.append({"role": "assistant", "content": "done"})
    events: list[JsonValue] = [
        {"type": "model.started", "payload": {}},
        {
            "type": "message.block.completed",
            "payload": {
                "block_type": "thinking",
                "content": "read skill",
            },
        },
        {
            "type": "tool.started",
            "payload": {
                "tool_call_id": "local-1",
                "tool_name": "skill_view",
                "arguments": {},
                "truncated": False,
            },
        },
        {
            "type": "tool.completed",
            "payload": {
                "tool_call_id": "local-1",
                "content": "skill result",
                "is_error": is_error,
                "truncated": False,
            },
        },
        {"type": "model.started", "payload": {}},
        {
            "type": "message.block.completed",
            "payload": {
                "block_type": "text",
                "content": "done",
            },
        },
    ]
    original = json.dumps(messages)
    assert _compact_simulation({"messages": messages}, events) == {
        "messages": [
            messages[0],
            {
                "role": "assistant",
                "tool_calls": [call],
                "reasoning": "read skill",
            },
            result,
            {"role": "assistant", "content": "done"},
        ]
    }
    assert json.dumps(messages) == original


@pytest.mark.parametrize("state", ["missing", "nested", "orphan", "no_event"])
def test_tool_result_supplement_requires_call_and_missing_result(state: str) -> None:
    """仅补有调用无结果的项，嵌套结果和孤立完成事件不重复插入。"""
    result: JsonValue = {
        "role": "tool",
        "id": "call-1",
        "requestor": "assistant",
        "content": "result",
        "error": False,
    }
    messages: list[JsonValue] = [{"role": "user", "content": "help"}]
    if state != "orphan":
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "skill_view",
                        "arguments": {},
                    }
                ],
            }
        )
    if state == "nested":
        messages.append({"role": "tool", "tool_messages": [result]})
    messages.append({"role": "assistant", "content": "done"})
    events: list[JsonValue] = (
        []
        if state == "no_event"
        else [
            {
                "type": "tool.completed",
                "payload": {
                    "tool_call_id": "call-1",
                    "content": "result",
                    "is_error": False,
                    "truncated": False,
                },
            }
        ]
    )
    expected = list(messages)
    if state == "missing":
        expected.insert(-1, result)
    assert _compact_simulation({"messages": messages}, events) == {"messages": expected}


@pytest.mark.parametrize("local_first", [False, True])
def test_mixed_candidate_calls_keep_local_call_and_result(local_first: bool) -> None:
    """同轮混合调用按 ID 合并，不能因首个调用匹配而遗漏或重复。"""
    bridge: dict[str, JsonValue] = {
        "id": "bridge",
        "name": "lookup",
        "arguments": {},
        "requestor": "assistant",
    }
    local: dict[str, JsonValue] = {
        "id": "local",
        "name": "skill_view",
        "arguments": {},
        "requestor": "assistant",
    }
    calls = [local, bridge] if local_first else [bridge, local]
    messages: list[JsonValue] = [
        {"role": "user", "content": "help"},
        {"role": "assistant", "tool_calls": [bridge]},
        {"role": "tool", "id": "bridge", "content": "original"},
        {"role": "assistant", "content": "done"},
    ]
    events: list[JsonValue] = [{"type": "model.started", "payload": {}}]
    for call in calls:
        events.append(
            {
                "type": "tool.started",
                "payload": {
                    "tool_call_id": call["id"],
                    "tool_name": call["name"],
                    "arguments": {},
                    "truncated": False,
                },
            }
        )
        events.append(
            {
                "type": "tool.completed",
                "payload": {
                    "tool_call_id": call["id"],
                    "content": "result",
                    "is_error": False,
                },
            }
        )
    assert _compact_simulation({"messages": messages}, events) == {
        "messages": [
            messages[0],
            {"role": "assistant", "tool_calls": calls},
            messages[2],
            {
                "role": "tool",
                "id": "local",
                "requestor": "assistant",
                "content": "result",
                "error": False,
            },
            messages[3],
        ]
    }


def test_missing_result_stays_with_its_call_in_split_messages() -> None:
    """同轮调用被 bridge 拆成多条消息时，结果不能越过下一条调用。"""
    messages: list[JsonValue] = [{"role": "user", "content": "help"}]
    events: list[JsonValue] = [{"type": "model.started", "payload": {}}]
    for call_id in ("a", "b"):
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "name": "lookup",
                        "arguments": {},
                    }
                ],
            }
        )
        events.append(
            {
                "type": "tool.started",
                "payload": {
                    "tool_call_id": call_id,
                    "tool_name": "lookup",
                    "arguments": {},
                    "truncated": False,
                },
            }
        )
        events.append(
            {
                "type": "tool.completed",
                "payload": {
                    "tool_call_id": call_id,
                    "content": "result",
                    "is_error": False,
                },
            }
        )
    messages.append({"role": "tool", "id": "b", "content": "original"})
    expected = list(messages)
    expected.insert(
        2,
        {
            "role": "tool",
            "id": "a",
            "requestor": "assistant",
            "content": "result",
            "error": False,
        },
    )
    assert _compact_simulation({"messages": messages}, events) == {"messages": expected}


def test_reasoning_only_output_does_not_displace_later_reply() -> None:
    """只有思考的中间调用不应使后续回复失配或导致正文重复。"""
    events: list[JsonValue] = [
        {"type": "model.started", "payload": {}},
        {
            "type": "message.block.completed",
            "payload": {"block_type": "thinking", "content": "继续分析"},
        },
        {"type": "model.started", "payload": {}},
        {
            "type": "message.block.completed",
            "payload": {"block_type": "thinking", "content": "得出结论"},
        },
        {
            "type": "message.block.completed",
            "payload": {"block_type": "text", "content": "完成"},
        },
    ]
    assert _compact_simulation(
        {
            "messages": [
                {"role": "user", "content": "继续"},
                {"role": "assistant", "content": "完成"},
            ]
        },
        events,
    ) == {
        "messages": [
            {"role": "user", "content": "继续"},
            {"role": "assistant", "reasoning": "继续分析"},
            {"role": "assistant", "content": "完成", "reasoning": "得出结论"},
        ]
    }


@pytest.mark.parametrize(
    "arguments,raw_arguments,truncated",
    [
        (None, None, False),
        (None, '{"name":"', False),
        ([], "[]", False),
        ("invalid", None, False),
        (None, '{"name":"', True),
    ],
)
def test_evaluator_officially_scores_structured_execution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: JsonValue,
    raw_arguments: JsonValue,
    truncated: bool,
) -> None:
    """Candidate 失败仍须由官方评分，并将完整诊断写入评估。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(
        json.dumps([{"id": "task_001", "evaluation_criteria": {}}]),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def official_evaluation(
        config: Tau3BankingConfig,
        request: object,
    ) -> EvaluationResponse:
        del config
        captured["request"] = request
        return EvaluationResponse(
            case_id="task_001",
            reward_info={
                "reward": 0.0,
                "info": {
                    "note": "Simulation terminated prematurely. "
                    "Termination reason: agent_error"
                },
            },
        )

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        official_evaluation,
    )
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="agent_error",
                tracelog={
                    "simulation": {
                        "termination_reason": "agent_error",
                        "messages": [],
                    },
                    "error": {
                        "code": "context_compaction_failed",
                        "message": "Context compaction failed",
                        "exception_type": "RuntimeError",
                        "stacktrace": (
                            "Traceback...\nRuntimeError: compaction failed\n"
                        ),
                        "error_id": "error-123",
                    },
                    "candidate_events": [
                        {"type": "model.started", "payload": {}},
                        {
                            "type": "tool.started",
                            "payload": {
                                "tool_call_id": "invalid-call",
                                "tool_name": "call_discoverable_agent_tool",
                                "arguments": arguments,
                                "raw_arguments": raw_arguments,
                                "truncated": truncated,
                            },
                        },
                        {
                            "type": "tool.completed",
                            "payload": {
                                "tool_call_id": "invalid-call",
                                "content": "Unterminated string",
                                "is_error": True,
                                "error_code": "invalid_tool_arguments",
                            },
                        },
                    ],
                },
                metadata=ExecutionMetadata(total_tokens=123, duration_seconds=4.5),
            ),
        )
    )

    case = (
        BankingEvaluator(_config(tmp_path))
        .evaluate(
            execution,
            dataset,
        )
        .cases[0]
    )

    assert case.result == "agent_error"
    assert case.score == 0
    assert case.feedback == ""
    assert case.tracelog == {
        "simulation": {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "invalid-call",
                            "name": "call_discoverable_agent_tool",
                            "arguments": arguments,
                            "raw_arguments": raw_arguments,
                            "truncated": truncated,
                            "requestor": "assistant",
                        }
                    ],
                },
                {
                    "role": "tool",
                    "id": "invalid-call",
                    "requestor": "assistant",
                    "content": "Unterminated string",
                    "error": True,
                    "error_code": "invalid_tool_arguments",
                },
            ]
        },
        "error": {
            "code": "context_compaction_failed",
            "message": "Context compaction failed",
            "exception_type": "RuntimeError",
            "stacktrace": "Traceback...\nRuntimeError: compaction failed\n",
            "error_id": "error-123",
        },
    }
    assert case.metadata.total_tokens == 123
    assert case.metadata.duration_seconds == 4.5
    request = captured["request"]
    assert isinstance(request, EvaluationRequest)
    assert request.simulation == {"termination_reason": "agent_error", "messages": []}


def test_evaluator_officially_scores_case_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 超时仍必须交给 Tau2 官方 Evaluator 评分。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(
        json.dumps([{"id": "task_001", "evaluation_criteria": {}}]),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def official_evaluation(
        config: Tau3BankingConfig,
        request: object,
    ) -> EvaluationResponse:
        del config
        captured["request"] = request
        return EvaluationResponse(
            case_id="task_001",
            reward_info={
                "reward": 0.0,
                "info": {
                    "note": "Simulation terminated prematurely. "
                    "Termination reason: timeout"
                },
            },
        )

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        official_evaluation,
    )
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="failed",
                tracelog={
                    "simulation": {
                        "id": "task_001-timeout",
                        "task_id": "task_001",
                        "start_time": "2026-09-03T20:00:00+08:00",
                        "end_time": "2026-09-03T20:20:00+08:00",
                        "duration": 1200,
                        "termination_reason": "timeout",
                        "messages": [],
                    },
                    "candidate_events": [],
                    "error": {
                        "code": "tau3_runner_timeout",
                        "message": "Tau3 Runner 执行超时",
                    },
                },
                metadata=ExecutionMetadata(total_tokens=0, duration_seconds=1200),
            ),
        )
    )

    case = (
        BankingEvaluator(_config(tmp_path))
        .evaluate(
            execution,
            dataset,
        )
        .cases[0]
    )

    assert case.result == "failed"
    assert case.score == 0
    assert case.feedback == ""
    assert case.tracelog == {
        "simulation": {"messages": []},
        "error": {
            "code": "tau3_runner_timeout",
            "message": "Tau3 Runner 执行超时",
        },
    }
    request = captured["request"]
    assert isinstance(request, EvaluationRequest)
    assert request.judge_llm == "openai/judge-model"
    assert request.simulation["termination_reason"] == "timeout"


def test_evaluator_excludes_prior_attempts_and_scores_only_final_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evaluation 只保留最终会话，重试历史留在 Execution 中。"""
    dataset = tmp_path / "train.json"
    dataset.write_text(
        json.dumps([{"id": "task_001", "evaluation_criteria": {}}]),
        encoding="utf-8",
    )
    captured: dict[str, EvaluationRequest] = {}

    def official_evaluation(
        config: Tau3BankingConfig,
        request: EvaluationRequest,
    ) -> EvaluationResponse:
        del config
        captured["request"] = request
        return EvaluationResponse(
            case_id="task_001",
            reward_info={"reward": 1.0},
        )

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        official_evaluation,
    )
    final_tracelog = {
        "simulation": {
            "termination_reason": "user_stop",
            "messages": [{"role": "user", "content": "done"}],
        },
        "candidate_events": [],
    }
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="completed",
                tracelog=final_tracelog,
                metadata=ExecutionMetadata(
                    total_tokens=105,
                    duration_seconds=4,
                ),
                attempts=(
                    CaseAttempt(
                        result="user_error",
                        tracelog={
                            "simulation": {"messages": ["large-body" * 10_000]},
                            "candidate_events": ["large-event" * 10_000],
                            "error": {
                                "code": "user_error",
                                "message": "wrong action",
                            },
                        },
                        metadata=ExecutionMetadata(
                            total_tokens=100,
                            duration_seconds=3.5,
                        ),
                    ),
                    CaseAttempt(
                        result="completed",
                        tracelog=final_tracelog,
                        metadata=ExecutionMetadata(
                            total_tokens=5,
                            duration_seconds=0.5,
                        ),
                    ),
                ),
            ),
        )
    )

    case = (
        BankingEvaluator(_config(tmp_path))
        .evaluate(
            execution,
            dataset,
        )
        .cases[0]
    )

    assert captured["request"].simulation == final_tracelog["simulation"]
    assert isinstance(case.tracelog, dict)
    assert case.tracelog == {
        "simulation": {"messages": [{"role": "user", "content": "done"}]}
    }
    assert "large-body" not in json.dumps(case.tracelog)
    assert "large-event" not in json.dumps(case.tracelog)


@pytest.mark.parametrize("last_reward", [0.5, 0.0])
@pytest.mark.parametrize("with_checks", [False, True])
def test_evaluator_averages_trials_and_reports_worst_trial_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    last_reward: float,
    with_checks: bool,
) -> None:
    """重复采样逐 Trial 官方评分取均值，反馈附最低分 Trial 的检查明细。"""
    dataset = tmp_path / "validation.json"
    dataset.write_text(
        json.dumps([{"id": "task_001", "evaluation_criteria": {}}]),
        encoding="utf-8",
    )
    rewards = {"trial-1": 1.0, "trial-2": 0.0, "trial-3": last_reward}
    seen: list[str] = []

    def official_evaluation(
        config: Tau3BankingConfig,
        request: EvaluationRequest,
    ) -> EvaluationResponse:
        del config
        simulation_id = str(request.simulation["id"])
        seen.append(simulation_id)
        reward = rewards[simulation_id]
        reward_info: dict[str, JsonValue] = {"reward": reward}
        db_checks = None
        if reward == 0.0 and with_checks:
            reward_info["action_checks"] = [
                {"action": {"name": "get_user"}, "action_match": False}
            ]
            db_checks = DBChecks(
                db_match=False,
                db_reward=0.0,
                diagnosis_status="differences_found",
            )
        return EvaluationResponse(
            case_id="task_001",
            reward_info=reward_info,
            db_checks=db_checks,
        )

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        official_evaluation,
    )

    def trial_tracelog(simulation_id: str) -> dict[str, JsonValue]:
        tracelog: dict[str, JsonValue] = {
            "simulation": {
                "id": simulation_id,
                "messages": [{"role": "assistant", "content": simulation_id}],
            },
            "candidate_events": [],
        }
        if simulation_id != "trial-1":
            tracelog["error"] = {"code": "provider_error", "message": simulation_id}
        return tracelog

    trials = tuple(
        CaseAttempt(
            result="completed" if index == 1 else "agent_error",
            tracelog=trial_tracelog(f"trial-{index}"),
            metadata=ExecutionMetadata(total_tokens=2, duration_seconds=0.5),
        )
        for index in (1, 2, 3)
    )
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="completed",
                tracelog=trial_tracelog("trial-1"),
                metadata=ExecutionMetadata(total_tokens=6, duration_seconds=1.5),
                trials=trials,
            ),
        )
    )

    case = BankingEvaluator(_config(tmp_path)).evaluate(execution, dataset).cases[0]

    assert seen == ["trial-1", "trial-2", "trial-3"]
    assert case.score == pytest.approx(sum(rewards.values()) / 3)
    feedback = json.loads(case.feedback)
    assert feedback["trial_rewards"] == [1.0, 0.0, last_reward]
    assert feedback["representative_trial"] == 2
    if with_checks:
        assert feedback["action_checks"] == [
            {"action": {"name": "get_user"}, "action_match": False}
        ]
        assert feedback["db_checks"]["db_match"] is False
    assert case.failure_kind == (
        "action_mismatch:get_user" if with_checks else "runner_error:provider_error"
    )
    assert case.result == "agent_error"
    assert case.tracelog == {
        "simulation": {"messages": [{"role": "assistant", "content": "trial-2"}]},
        "error": {"code": "provider_error", "message": "trial-2"},
    }
    assert case.metadata == ExecutionMetadata(total_tokens=6, duration_seconds=1.5)
    assert execution.cases[0].tracelog == trial_tracelog("trial-1")


def test_evaluator_rejects_trial_without_simulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一 Trial 缺少 simulation 都不得静默跳过评分。"""
    dataset = tmp_path / "validation.json"
    dataset.write_text(
        json.dumps([{"id": "task_001", "evaluation_criteria": {}}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.evaluator.evaluate_tau2",
        lambda config, request: EvaluationResponse(
            case_id="task_001", reward_info={"reward": 1.0}
        ),
    )
    valid: dict[str, JsonValue] = {"simulation": {"messages": []}}
    trials = (
        CaseAttempt(result="completed", tracelog=valid, metadata=_metadata()),
        CaseAttempt(result="completed", tracelog={}, metadata=_metadata()),
    )
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="task_001",
                result="completed",
                tracelog=valid,
                metadata=ExecutionMetadata(total_tokens=6, duration_seconds=1),
                trials=trials,
            ),
        )
    )

    with pytest.raises(ValueError, match="simulation"):
        BankingEvaluator(_config(tmp_path)).evaluate(execution, dataset)


def _metadata() -> ExecutionMetadata:
    return ExecutionMetadata(total_tokens=3, duration_seconds=0.5)


def _config(tmp_path: Path) -> Tau3BankingConfig:
    tau2 = tmp_path / "tau2"
    tau2.mkdir()
    (tau2 / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.1"\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("redis: {}\n", encoding="utf-8")
    return Tau3BankingConfig(
        tau2_project=tau2,
        runtime_config=runtime,
        judge_llm="openai/judge-model",
        user_llm="openai/user",
        host="127.0.0.1",
        agent_id="banking-agent",
        startup_timeout_seconds=1,
        case_timeout_seconds=1,
        shutdown_timeout_seconds=1,
    )
