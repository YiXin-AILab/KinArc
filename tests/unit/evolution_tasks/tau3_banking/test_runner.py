"""Tau2 Runner 配置边界测试。"""

import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Literal

import pytest
from agentsystem.common.errors import AdapterError
from evolution_tasks.tau3_banking._internal.process import (
    EvaluationRequest,
    RunnerError,
    RunnerRequest,
)
from evolution_tasks.tau3_banking._internal.runner import evaluate_case, run_case


@pytest.mark.parametrize("failure_kind", ["runtime", "deadline", "cleanup"])
def test_runner_preserves_partial_evidence_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """运行或清理失败都不能将已有官方轨迹替换成空 simulation。"""
    trajectory = [{"role": "user", "content": "partial conversation"}]
    events = [{"type": "tool.started", "payload": {"tool_call_id": "call"}}]
    session = SimpleNamespace(
        candidate_events=events,
        failure=None,
        close=lambda: None,
    )

    class Simulation:
        def __init__(self, **values: object) -> None:
            self.values = values
            self.duration = 0.1
            self.reward_info: object = None

        def model_dump(self, *, mode: str) -> dict[str, object]:
            return self.values

    class Orchestrator:
        def run(self) -> Simulation:
            if failure_kind == "cleanup":
                return Simulation(termination_reason="user_stop", messages=trajectory)
            raise AdapterError(
                operation="test",
                code=(
                    "tau3_case_timeout" if failure_kind == "deadline" else "llm_timeout"
                ),
                message="original failure",
            )

        def get_trajectory(self) -> list[dict[str, str]]:
            return trajectory

    def close_server() -> None:
        if failure_kind == "cleanup":
            raise TimeoutError("bridge close timeout")

    attributes: dict[tuple[str, str], object] = {
        ("tau2.data_model.tasks", "Task"): SimpleNamespace(
            model_validate=lambda case: SimpleNamespace(id=case["id"])
        ),
        ("tau2.data_model.simulation", "TextRunConfig"): lambda **kwargs: kwargs,
        ("tau2.data_model.simulation", "SimulationRun"): Simulation,
        ("tau2.runner", "build_text_orchestrator"): lambda config, task: Orchestrator(),
        ("tau2.registry", "registry"): SimpleNamespace(
            register_agent_factory=lambda factory, name: None
        ),
        ("tau2.utils.utils", "get_now"): lambda: "2026-09-05T00:00:00",
    }
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._require_tau2_version",
        lambda: None,
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._attribute",
        lambda module, name: attributes[(module, name)],
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner.AgentsystemHttpSession",
        lambda **_: session,
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner.ToolBridgeServer",
        lambda *args: SimpleNamespace(start=lambda: None, close=close_server),
    )
    result = run_case(
        RunnerRequest(
            case={"id": "task"},
            tool_bridge_socket=Path("/tmp/test.sock"),
            base_url="http://unused",
            app_id="a",
            agent_id="a",
            user_id="u",
            user_llm="unused",
            timeout_seconds=2,
        )
    )
    assert result.simulation["messages"] == trajectory
    assert list(result.candidate_events) == events
    assert result.error is not None
    assert (
        result.simulation["termination_reason"]
        == {
            "runtime": "agent_error",
            "deadline": "timeout",
            "cleanup": "user_stop",
        }[failure_kind]
    )


@pytest.mark.parametrize("retrieval", ["bm25_grep", "golden_retrieval"])
def test_run_case_configures_retrieval_and_user_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retrieval: Literal["bm25_grep", "golden_retrieval"],
) -> None:
    """检索方式匹配 Candidate 工具，用户请求具有独立于 case 的超时。"""
    captured: dict[str, object] = {}

    class FakeTaskType:
        @staticmethod
        def model_validate(case: dict[str, object]) -> SimpleNamespace:
            return SimpleNamespace(id=case["id"])

    class FakeConfigType:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeRegistry:
        def register_agent_factory(
            self, factory: Callable[..., object], name: str
        ) -> None:
            factory(
                [],
                "official policy\n<required_documents>case only</required_documents>",
            )

    class FakeSimulation:
        reward_info: object | None = object()
        duration = 0.25

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"termination_reason": "user_stop"}

    class FakeOrchestrator:
        environment: object = SimpleNamespace()

        def run(self) -> FakeSimulation:
            assert type(self.environment) is SimpleNamespace
            return FakeSimulation()

    attributes: dict[tuple[str, str], object] = {
        ("tau2.data_model.tasks", "Task"): FakeTaskType,
        ("tau2.data_model.simulation", "TextRunConfig"): FakeConfigType,
        ("tau2.runner", "build_text_orchestrator"): (
            lambda config, task: FakeOrchestrator()
        ),
        ("tau2.registry", "registry"): FakeRegistry(),
        ("tau2.utils.utils", "get_now"): lambda: "replayed",
    }
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._require_tau2_version",
        lambda: None,
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner.create_http_agent",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._attribute",
        lambda module, name: attributes[(module, name)],
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner.AgentsystemHttpSession",
        lambda **kwargs: SimpleNamespace(
            candidate_events=[], failure=None, close=lambda: None
        ),
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner.ToolBridgeServer",
        lambda bridge, socket_path: SimpleNamespace(
            start=lambda: None, close=lambda: None
        ),
    )
    request = RunnerRequest(
        case={"id": "task_001"},
        tool_bridge_socket=tmp_path / "tools.sock",
        retrieval_config=retrieval,
        base_url="http://127.0.0.1:32123",
        app_id="tau3-banking",
        agent_id="banking-agent",
        user_id="user-1",
        user_llm="openai/user-model",
        timeout_seconds=30,
    )

    run_case(request)

    assert captured["retrieval_config"] == retrieval
    assert captured["llm_args_user"] == {"temperature": 0.0, "timeout": 180}
    policy_path = request.tool_bridge_socket.with_suffix(".policy.md")
    if retrieval == "golden_retrieval":
        assert policy_path.read_text() == (
            "official policy\n<required_documents>case only</required_documents>"
        )
        assert policy_path.stat().st_mode & 0o777 == 0o600
    else:
        assert not policy_path.exists()


def test_run_case_returns_public_candidate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate run.failed 必须通过 JSON 协议返回，而不是使 runner 崩溃。"""

    class FakeTaskType:
        @staticmethod
        def model_validate(case: dict[str, object]) -> SimpleNamespace:
            return SimpleNamespace(id=case["id"])

    class FakeRegistry:
        def register_agent_factory(self, factory: object, name: str) -> None:
            del factory, name

    failure = RunnerError(
        code="context_compaction_failed",
        message="Context compaction failed",
        exception_type="RuntimeError",
        stacktrace="Traceback...\nRuntimeError: compaction failed\n",
        error_id="error-123",
    )

    class FakeSimulation:
        reward_info: object | None = object()
        duration = 4.5

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"termination_reason": "agent_error", "messages": []}

    class FakeOrchestrator:
        environment: object = SimpleNamespace()

        def run(self) -> FakeSimulation:
            return FakeSimulation()

    attributes: dict[tuple[str, str], object] = {
        ("tau2.data_model.tasks", "Task"): FakeTaskType,
        ("tau2.data_model.simulation", "TextRunConfig"): lambda **kwargs: kwargs,
        ("tau2.runner", "build_text_orchestrator"): (
            lambda config, task: FakeOrchestrator()
        ),
        ("tau2.registry", "registry"): FakeRegistry(),
        ("tau2.utils.utils", "get_now"): lambda: "replayed",
    }
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._require_tau2_version",
        lambda: None,
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._attribute",
        lambda module, name: attributes[(module, name)],
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner.AgentsystemHttpSession",
        lambda **kwargs: SimpleNamespace(
            candidate_events=[{"type": "run.failed", "payload": {}}],
            failure=failure,
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner.ToolBridgeServer",
        lambda bridge, socket_path: SimpleNamespace(
            start=lambda: None, close=lambda: None
        ),
    )

    result = run_case(
        RunnerRequest(
            case={"id": "task_001"},
            tool_bridge_socket=Path("/tmp/test-tau2-tools.sock"),
            base_url="http://127.0.0.1:32123",
            app_id="tau3-banking",
            agent_id="banking-agent",
            user_id="user-1",
            user_llm="openai/user-model",
            timeout_seconds=30,
        )
    )

    assert result.simulation == {
        "termination_reason": "agent_error",
        "messages": [],
    }
    assert result.error is not None
    assert result.error.code == "context_compaction_failed"
    assert result.error.message == "Context compaction failed"
    assert result.error.exception_type == "RuntimeError"
    assert result.error.stacktrace == (
        "Traceback...\nRuntimeError: compaction failed\n"
    )
    assert result.error.error_id == "error-123"


@pytest.mark.parametrize("retrieval", ["bm25_grep", "golden_retrieval"])
def test_evaluate_case_uses_official_read_allowlist_and_retrieval(
    monkeypatch: pytest.MonkeyPatch,
    retrieval: Literal["bm25_grep", "golden_retrieval"],
) -> None:
    """重评必须传入官方 task 派生的 read log allowlist。"""
    captured: dict[str, object] = {}

    class FakeModelType:
        @staticmethod
        def model_validate(value: dict[str, object]) -> SimpleNamespace:
            return SimpleNamespace(**value)

    class FakeEvaluationType:
        ALL_WITH_NL_ASSERTIONS = "all"

    class FakeReward:
        db_check = SimpleNamespace(db_match=False, db_reward=0.0)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"reward": 1.0}

    class FakeEnvironmentEvaluator:
        @classmethod
        def calculate_reward(cls, **kwargs: object) -> FakeReward:
            return FakeReward()

    def fake_evaluate_simulation(**kwargs: object) -> FakeReward:
        assert vars(nl_evaluator)["DEFAULT_LLM_NL_ASSERTIONS"] == "openai/judge-model"
        captured.update(kwargs)
        return FakeEnvironmentEvaluator.calculate_reward(
            environment_constructor=lambda: None
        )

    nl_evaluator = ModuleType("tau2.evaluator.evaluator_nl_assertions")
    monkeypatch.setattr(
        nl_evaluator, "DEFAULT_LLM_NL_ASSERTIONS", "unavailable-model", raising=False
    )
    monkeypatch.setitem(sys.modules, nl_evaluator.__name__, nl_evaluator)

    attributes: dict[tuple[str, str], object] = {
        (
            "tau2.evaluator.evaluator_env",
            "EnvironmentEvaluator",
        ): FakeEnvironmentEvaluator,
        ("tau2.data_model.tasks", "Task"): FakeModelType,
        ("tau2.data_model.simulation", "SimulationRun"): FakeModelType,
        ("tau2.evaluator.evaluator", "EvaluationType"): FakeEvaluationType,
        ("tau2.evaluator.evaluator", "evaluate_simulation"): (fake_evaluate_simulation),
        ("tau2.runner.build", "_derive_read_log_allowlist"): (
            lambda task: {"get_account"}
        ),
    }
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._require_tau2_version",
        lambda: None,
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.runner._attribute",
        lambda module, name: attributes[(module, name)],
    )

    response = evaluate_case(
        EvaluationRequest(
            case={"id": "task_001"},
            retrieval_config=retrieval,
            simulation={"termination_reason": "user_stop"},
            judge_llm="openai/judge-model",
        )
    )

    assert captured["env_kwargs"] == {
        "retrieval_variant": retrieval,
        "read_log_allowlist": {"get_account"},
    }
    assert response.db_checks is not None
    assert response.db_checks.db_match is False
    assert response.db_checks.diagnosis_status == "unavailable"
