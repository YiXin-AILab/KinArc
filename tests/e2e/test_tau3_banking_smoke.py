"""Tau3 Banking 真实单 Case 显式 smoke test。"""

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest
from evolution.models import DatasetRole
from evolution.task import load_json_cases
from evolution_tasks.tau3_banking._internal.process import load_banking_config
from evolution_tasks.tau3_banking.evaluator import BankingEvaluator
from evolution_tasks.tau3_banking.executor import BankingExecutor

PROJECT_ROOT = Path(__file__).parents[2]
TASK_DIR = PROJECT_ROOT / "evolution_tasks" / "tau3_banking"


def test_one_real_banking_case(tmp_path: Path) -> None:
    """真实 Candidate、Tau2 User 与官方 Evaluator 应完成一条持久轨迹。"""
    _require_smoke_environment()
    config = load_banking_config(
        TASK_DIR,
        {
            "runtime_config": "../../config/runtime.yaml",
            "user_llm": os.environ["TAU3_USER_LLM"],
            "judge_llm": os.environ["TAU3_JUDGE_LLM"],
        },
        entry_dir=TASK_DIR,
    )
    train_case = load_json_cases(TASK_DIR / "datasets" / "train.json")[0]
    dataset = tmp_path / "train-one.json"
    dataset.write_text(
        json.dumps([train_case.content], ensure_ascii=False),
        encoding="utf-8",
    )
    executor = BankingExecutor(config=config, project_root=PROJECT_ROOT)
    evaluator = BankingEvaluator(config)
    candidate_database = (
        PROJECT_ROOT / "application" / "banking_agent" / "data" / "db.json"
    )
    database_before = candidate_database.read_bytes()

    execution = executor.execute(
        PROJECT_ROOT / "application" / "banking_agent",
        dataset,
        DatasetRole.TRAIN,
    )
    evaluation = evaluator.evaluate(execution, dataset)

    assert execution.cases[0].id == train_case.id
    assert evaluation.cases[0].id == train_case.id
    assert isinstance(execution.cases[0].tracelog, dict)
    tracelog = execution.cases[0].tracelog
    assert tracelog["candidate_events"]
    assert tracelog["simulation"]
    assert candidate_database.read_bytes() == database_before
    assert _candidate_tool_ids(tracelog) <= _simulation_tool_ids(tracelog)
    _assert_candidate_tool_result_order(tracelog)


def _candidate_tool_ids(tracelog: Mapping[str, object]) -> set[str]:
    events = tracelog["candidate_events"]
    assert isinstance(events, list)
    ids = {
        payload["tool_call_id"]
        for event in events
        if isinstance(event, dict)
        and event.get("type") == "tool.started"
        and isinstance((payload := event.get("payload")), dict)
        and isinstance(payload.get("tool_call_id"), str)
    }
    assert ids
    return ids


def _simulation_tool_ids(tracelog: Mapping[str, object]) -> set[str]:
    simulation = tracelog["simulation"]
    assert isinstance(simulation, dict)
    messages = simulation.get("messages")
    assert isinstance(messages, list)
    return {
        tool_call["id"]
        for message in messages
        if isinstance(message, dict)
        and isinstance((tool_calls := message.get("tool_calls")), list)
        for tool_call in tool_calls
        if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str)
    }


def _assert_candidate_tool_result_order(tracelog: Mapping[str, object]) -> None:
    """确认每个 Candidate ToolMessage 位于对应 Assistant ToolCall 之后。"""
    simulation = tracelog["simulation"]
    assert isinstance(simulation, dict)
    messages = simulation.get("messages")
    assert isinstance(messages, list)
    for tool_call_id in _candidate_tool_ids(tracelog):
        call_positions = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict)
            and isinstance(message.get("tool_calls"), list)
            and any(
                isinstance(tool_call, dict) and tool_call.get("id") == tool_call_id
                for tool_call in message["tool_calls"]
            )
        ]
        result_positions = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and message.get("id") == tool_call_id
        ]
        assert len(call_positions) == 1
        assert len(result_positions) == 1
        assert call_positions[0] < result_positions[0]


def _require_smoke_environment() -> None:
    if os.environ.get("TAU3_SMOKE") != "1":
        pytest.skip("需要显式设置 TAU3_SMOKE=1")
    required = (
        "TAU3_USER_LLM",
        "TAU3_JUDGE_LLM",
        "TEST_AGENT_LLM_MODEL",
        "TEST_AGENT_LLM_API_KEY",
        "TEST_AGENT_LLM_BASE_URL",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_USERNAME",
        "REDIS_PASSWORD",
        "REDIS_DATABASE",
    )
    missing = [name for name in required if name not in os.environ]
    if missing:
        pytest.skip("缺少 smoke 环境变量：" + ", ".join(missing))
