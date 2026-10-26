"""在 Tau2 1.0.1 uv 环境中执行或重评一个 Banking Case。"""

import argparse
import importlib
import importlib.metadata
import json
import logging
import os
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from agentsystem.common.errors import AdapterError, sanitized_exc_info
from agentsystem.common.logging import log_unexpected_exception
from pydantic import JsonValue

from evolution_tasks.tau3_banking._internal.adapter import (
    AgentsystemHttpSession,
    create_http_agent,
)
from evolution_tasks.tau3_banking._internal.process import (
    DBChecks,
    DBDifference,
    EvaluationRequest,
    EvaluationResponse,
    RunnerError,
    RunnerRequest,
    RunnerResult,
)
from evolution_tasks.tau3_banking._internal.tool_bridge import (
    CaseToolBridge,
    ToolBridgeServer,
)

_AGENT_NAME = "agentsystem_banking_http"
LOGGER = logging.getLogger(__name__)


def run_case(request: RunnerRequest) -> RunnerResult:
    """使用官方 Orchestrator 生成一条未评分 SimulationRun。"""
    _require_tau2_version()
    task_type = _attribute("tau2.data_model.tasks", "Task")
    config_type = _attribute("tau2.data_model.simulation", "TextRunConfig")
    build_orchestrator = _attribute("tau2.runner", "build_text_orchestrator")
    registry = _attribute("tau2.registry", "registry")
    get_now = _attribute("tau2.utils.utils", "get_now")
    task = task_type.model_validate(request.case)
    bridge = CaseToolBridge()
    session = AgentsystemHttpSession(
        base_url=request.base_url,
        app_id=request.app_id,
        agent_id=request.agent_id,
        user_id=request.user_id,
        session_id=f"tau3-{request.case['id']}-{uuid4().hex}",
        timeout_seconds=request.timeout_seconds,
    )

    def factory(tools: list[object], domain_policy: str, **kwargs: object) -> object:
        """创建当前 Case 的 HTTP Agent；Golden 模式另写入官方策略文件。"""
        del kwargs
        if request.retrieval_config == "golden_retrieval":
            policy_path = request.tool_bridge_socket.with_suffix(".policy.md")
            policy_path.touch(mode=0o600, exist_ok=False)
            policy_path.write_text(domain_policy, encoding="utf-8")
        return create_http_agent(
            tools=tools,
            domain_policy=domain_policy,
            session=session,
            bridge=bridge,
        )

    registry.register_agent_factory(factory, _AGENT_NAME)
    config = config_type(
        domain="banking_knowledge",
        agent=_AGENT_NAME,
        user="user_simulator",
        llm_user=request.user_llm,
        llm_args_user={"temperature": 0.0, "timeout": 180},
        retrieval_config=request.retrieval_config,
        num_trials=1,
        max_retries=0,
        timeout=request.timeout_seconds,
        enforce_communication_protocol=True,
    )
    orchestrator = build_orchestrator(config, task)
    started_at = get_now()
    started = time.monotonic()
    server = ToolBridgeServer(bridge, request.tool_bridge_socket)
    server.start()
    try:
        try:
            simulation = orchestrator.run()
        except Exception as error:
            if isinstance(error, AdapterError):
                failure = RunnerError(code=error.code, message=error.message)
            else:
                error_id = log_unexpected_exception(
                    LOGGER, "tau3_runner_failed", error, operation="tau3.runner"
                )
                failure = RunnerError(
                    code="tau3_runner_failed",
                    message="Tau2 执行异常，已保留部分轨迹",
                    exception_type=type(error).__name__,
                    stacktrace="".join(
                        traceback.format_exception(*sanitized_exc_info(error))
                    ),
                    error_id=error_id,
                )
            session.failure = session.failure or failure
            simulation_type = _attribute("tau2.data_model.simulation", "SimulationRun")
            simulation = simulation_type(
                id=getattr(orchestrator, "simulation_id", None) or uuid4().hex,
                task_id=task.id,
                start_time=getattr(orchestrator, "_run_start_time", None) or started_at,
                end_time=get_now(),
                duration=time.monotonic() - started,
                termination_reason=(
                    "timeout" if failure.code == "tau3_case_timeout" else "agent_error"
                ),
                messages=orchestrator.get_trajectory(),
                seed=getattr(orchestrator, "seed", None),
            )
        finally:
            bridge.close()
            session.close()
    finally:
        try:
            server.close()
        except Exception as error:
            log_unexpected_exception(
                LOGGER,
                "tau3_bridge_cleanup_failed",
                error,
                operation="tau3.bridge.close",
            )
            session.failure = session.failure or RunnerError(
                code="tau3_bridge_cleanup_failed",
                message="Bridge 清理失败，已保留原始 simulation",
            )
    simulation.reward_info = None
    return RunnerResult(
        case_id=task.id,
        simulation=simulation.model_dump(mode="json"),
        error=session.failure,
        candidate_events=tuple(session.candidate_events),
        total_tokens=_total_tokens(session.candidate_events),
        duration_seconds=float(simulation.duration),
    )


def evaluate_case(request: EvaluationRequest) -> EvaluationResponse:
    """调用 Tau2 官方 ALL_WITH_NL_ASSERTIONS Evaluator。"""
    _require_tau2_version()
    # Tau2 1.0.1 从评分模块常量读取模型；每个评分子进程只处理一个请求。
    nl_evaluator = importlib.import_module("tau2.evaluator.evaluator_nl_assertions")
    vars(nl_evaluator)["DEFAULT_LLM_NL_ASSERTIONS"] = request.judge_llm
    task_type = _attribute("tau2.data_model.tasks", "Task")
    simulation_type = _attribute("tau2.data_model.simulation", "SimulationRun")
    evaluation_type = _attribute("tau2.evaluator.evaluator", "EvaluationType")
    evaluate_simulation = _attribute("tau2.evaluator.evaluator", "evaluate_simulation")
    derive_read_log_allowlist = _attribute(
        "tau2.runner.build",
        "_derive_read_log_allowlist",
    )
    task = task_type.model_validate(request.case)
    simulation = simulation_type.model_validate(request.simulation)
    env_evaluator = _attribute("tau2.evaluator.evaluator_env", "EnvironmentEvaluator")
    with capture_db_checks(env_evaluator) as db_checks:
        reward = evaluate_simulation(
            simulation=simulation,
            task=task,
            evaluation_type=evaluation_type.ALL_WITH_NL_ASSERTIONS,
            solo_mode=False,
            domain="banking_knowledge",
            env_kwargs={
                "retrieval_variant": request.retrieval_config,
                "read_log_allowlist": derive_read_log_allowlist(task),
            },
        )
    return EvaluationResponse(
        case_id=task.id,
        reward_info=reward.model_dump(mode="json"),
        db_checks=db_checks[0] if db_checks else None,
    )


class _ObservedEnvironment:
    """透传官方环境操作，并在 hash 时刻复制其输入。"""

    def __init__(self, environment: Any) -> None:
        """绑定官方环境并初始化 hash 时刻的数据库快照容器。"""
        self.environment = environment
        self.snapshots: dict[str, JsonValue] = {}

    def __getattr__(self, name: str) -> Any:
        """将未包装的属性访问直接交给官方环境。"""
        return getattr(self.environment, name)

    def get_db_hash(self) -> str | None:
        """观察 agent DB，原样返回官方 hash。"""
        self._snapshot("agent", self.environment.tools)
        return cast(str | None, self.environment.get_db_hash())

    def get_user_db_hash(self) -> str | None:
        """观察 user DB，原样返回官方 hash。"""
        self._snapshot("user", self.environment.user_tools)
        return cast(str | None, self.environment.get_user_db_hash())

    def _snapshot(self, database: str, tools: Any) -> None:
        # Tau2 ToolKitBase.get_db_hash 使用 model_dump + default=str。
        # JSON roundtrip 同时隔离后续修改，并保留 int/float 的表示差异。
        """在官方 hash 调用前复制数据库的 JSON 表示，避免后续修改污染证据。"""
        self.snapshots[database] = (
            json.loads(json.dumps(tools.db.model_dump(), sort_keys=True, default=str))
            if tools is not None
            else None
        )


@contextmanager
def capture_db_checks(evaluator: Any) -> Iterator[list[DBChecks]]:
    """观察一次官方 DB 评分所使用的环境快照。

    临时替换评分类的方法，并在退出时恢复原描述符。仅用于一请求一子进程，
    不可在同一进程内并发调用；不额外重放轨迹，也不修改官方 reward。

    Args:
        evaluator: 提供静态 calculate_reward 方法的官方环境评分类。

    Yields:
        在评分调用完成后填入 DB 检查结果的列表。
    """
    original_descriptor = vars(evaluator)["calculate_reward"]
    original = evaluator.calculate_reward
    checks: list[DBChecks] = []

    def calculate_reward(**kwargs: Any) -> Any:
        """包装环境构造器观察 DB 快照，原样返回官方评分结果。"""
        constructor = kwargs["environment_constructor"]
        environments: list[_ObservedEnvironment] = []

        def observed_constructor(**env_kwargs: Any) -> _ObservedEnvironment:
            """创建官方环境并登记其只读观察包装器。"""
            environment = _ObservedEnvironment(constructor(**env_kwargs))
            environments.append(environment)
            return environment

        reward = original(**{**kwargs, "environment_constructor": observed_constructor})
        if reward.db_check is not None:
            checks.append(_build_checks(reward.db_check, environments))
        return reward

    evaluator.calculate_reward = staticmethod(calculate_reward)
    try:
        yield checks
    finally:
        evaluator.calculate_reward = original_descriptor


def _build_checks(check: Any, environments: list[_ObservedEnvironment]) -> DBChecks:
    """将官方 DB 判定与已捕获快照合并为差异反馈。"""
    differences: list[DBDifference] = []
    # Tau2 1.0.1 的 ENV evaluator 顺序固定为 predicted、gold。
    if not check.db_match and len(environments) == 2:
        actual, expected = environments
        databases: tuple[Literal["agent", "user"], ...] = ("agent", "user")
        for database in databases:
            if database in expected.snapshots and database in actual.snapshots:
                differences.extend(
                    _diff(
                        expected.snapshots[database],
                        actual.snapshots[database],
                        database,
                    )
                )
    return DBChecks(
        db_match=check.db_match,
        db_reward=check.db_reward,
        diagnosis_status=(
            "matched"
            if check.db_match
            else "differences_found"
            if differences
            else "unavailable"
        ),
        differences=differences,
    )


def _diff(
    expected: JsonValue,
    actual: JsonValue,
    database: Literal["agent", "user"],
    path: str = "",
) -> Iterator[DBDifference]:
    """递归比较官方 DB 快照的 JSON 表示。

    Args:
        expected: 官方预期状态的 JSON 值。
        actual: 实际执行状态的 JSON 值。
        database: 该快照来自 agent 还是 user 数据库。
        path: 当前节点的 JSON Pointer，根节点为空字符串。

    Yields:
        不同节点的值及存在标志，区分字段缺失、null 和数值表示差异。
    """
    if json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True):
        return
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_items, actual_items = expected, actual
    elif isinstance(expected, list) and isinstance(actual, list):
        expected_items = {str(index): value for index, value in enumerate(expected)}
        actual_items = {str(index): value for index, value in enumerate(actual)}
    else:
        yield DBDifference(
            database=database, path=path, expected=expected, actual=actual
        )
        return
    for key in sorted(expected_items.keys() | actual_items.keys()):
        child_path = path + "/" + key.replace("~", "~0").replace("/", "~1")
        if key in expected_items and key in actual_items:
            yield from _diff(
                expected_items[key], actual_items[key], database, child_path
            )
        else:
            yield DBDifference(
                database=database,
                path=child_path,
                expected_present=key in expected_items,
                actual_present=key in actual_items,
                expected=expected_items.get(key),
                actual=actual_items.get(key),
            )


def main() -> None:
    """读取一个显式 JSON 请求并原子发布对应结果。"""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request", type=Path)
    mode.add_argument("--evaluation-request", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--evaluation-result", type=Path)
    arguments = parser.parse_args()
    if arguments.request is not None:
        if arguments.result is None:
            parser.error("--request 要求 --result")
        runner_request = RunnerRequest.model_validate_json(
            arguments.request.read_text(encoding="utf-8")
        )
        _atomic_write(arguments.result, run_case(runner_request).model_dump_json())
        return
    if arguments.evaluation_result is None:
        parser.error("--evaluation-request 要求 --evaluation-result")
    evaluation_request = EvaluationRequest.model_validate_json(
        arguments.evaluation_request.read_text(encoding="utf-8")
    )
    _atomic_write(
        arguments.evaluation_result,
        evaluate_case(evaluation_request).model_dump_json(),
    )


def _require_tau2_version() -> None:
    """拒绝使用与当前适配实现不匹配的 Tau2 版本。"""
    version = importlib.metadata.version("tau2")
    if version != "1.0.1":
        raise RuntimeError(f"Tau2 版本必须是 1.0.1，当前为 {version}")


def _attribute(module_name: str, attribute: str) -> Any:
    """延迟读取 Tau2 模块属性，使导入入口不要求预先加载 Tau2。"""
    return getattr(importlib.import_module(module_name), attribute)


def _total_tokens(events: list[dict[str, object]]) -> int:
    """读取最近一次 run 终态的有效 token 用量，缺失时返回零。"""
    for event in reversed(events):
        if event.get("type") not in {
            "run.completed",
            "run.failed",
            "run.canceled",
        }:
            continue
        payload = event.get("payload")
        usage = payload.get("usage") if isinstance(payload, dict) else None
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
    return 0


def _atomic_write(path: Path, content: str) -> None:
    """先写同目录临时文件，再原子发布 Runner JSON 结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
