"""使用确定性且不实际评分的夹具验证冻结、评测、恢复和报告。"""

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
)
from benchmarks.evoagentbench.common.identity import file_digest
from benchmarks.evoagentbench.data.prepare_dataset import pilot_ids, split_ids
from benchmarks.evoagentbench.experiments import experiment, experiment_state
from benchmarks.evoagentbench.experiments.experiment_state import (
    ExperimentConfig,
    freeze,
    initialize,
    protocol_inputs,
    write_json,
)
from benchmarks.evoagentbench.reporting.experiment_report import (
    build_report,
    learning_cost,
)
from evolution.candidates import tree_digest
from evolution.models import (
    CaseAttempt,
    CaseEvaluation,
    CaseExecution,
    DatasetRole,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
)
from evolution.task import TaskConfig


def test_pilot_holdout_never_uses_formal_validation_or_test() -> None:
    original = {
        "train": [str(i) for i in range(182)],
        "test": [f"test-{i}" for i in range(86)],
    }
    formal = split_ids(original, 36, 42)
    pilot = pilot_ids(original, 36, 42)
    seen = [case for values in pilot.values() for case in values]
    assert [len(pilot[r]) for r in ("train", "validation", "test")] == [20, 8, 12]
    assert len(set(seen)) == 40
    assert set(seen) <= set(formal["train"])


def prepared_experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        experiment_state, "dependency_identity", lambda _: "fixture-dependencies"
    )
    monkeypatch.setenv("FIXTURE_API_KEY", "fixture-token")
    monkeypatch.setenv("FIXTURE_API_BASE", "https://fixture.invalid")
    monkeypatch.setenv("EVOLUTION_LLM_MODEL", "openai/fixture")
    monkeypatch.setenv("EVOLUTION_LLM_API_KEY", "fixture-token")
    monkeypatch.setenv("EVOLUTION_LLM_BASE_URL", "https://fixture.invalid")
    template = tmp_path / "template"
    target = template / "agent"
    (target / "config").mkdir(parents=True)
    (target / "prompt").mkdir()
    (target / "prompt/system.md").write_text("seed")
    agent = AgentConfig(
        agent_id="fixture",
        agent_name="fixture",
        prompt="prompt/system.md",
        outbound=AgentOutboundConfig(type="log"),
        llm_model=ModelParamsConfig(
            model="openai/fixture",
            base_url="https://fixture.invalid",
            temperature=0.6,
            max_output_tokens=16384,
            context_window=128000,
        ),
    )
    (target / "config/agent.yaml").write_text(
        yaml.safe_dump(agent.model_dump(mode="json"))
    )
    (template / "executor.py").write_text("# fixture")
    (template / "evaluator.py").write_text("# fixture")
    (template / "runtime.yaml").write_text("trace:\n  enabled: false\n")
    (template / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "objective": "fixture",
                "initial_target": "agent",
                "workspace": {"read_only": ["config/"]},
                "settings": {"runtime_config": "runtime.yaml"},
            }
        )
    )
    base = tmp_path / "baseline.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "nanobot_version": "0.1.4.post3",
                "max_retries": 0,
                "agent": {
                    "model": "custom/fixture",
                    "provider": "custom",
                    "providers": {
                        "custom": {
                            "apiKey": "${FIXTURE_API_KEY}",
                            "apiBase": "${FIXTURE_API_BASE}",
                        }
                    },
                    "temperature": 0.6,
                    "maxTokens": 16384,
                    "contextWindowTokens": 128000,
                },
            }
        )
    )
    root = tmp_path / "experiment"
    initialize(root, template, base, ExperimentConfig())
    # 此夹具有意保留历史协议中的四次采样设置。
    task_path = root / "task/config.yaml"
    task_raw = yaml.safe_load(task_path.read_text())
    task_raw["settings"]["test_trials"] = 4
    task_path.write_text(yaml.safe_dump(task_raw))
    for role in ("train", "validation", "test"):
        write_json(
            root / "task/datasets" / f"{role}.json",
            {"cases": [{"id": "one", "prompt": "fixture"}]},
        )
    for candidate in ("seed", "best"):
        dest = root / "task/.evolution/candidates" / candidate / "target"
        dest.parent.mkdir(parents=True)
        shutil.copytree(root / "initial_target", dest)
    (root / "task/.evolution/candidates/best/target/prompt/system.md").write_text(
        "evolved"
    )
    write_json(
        root / "optimization.json",
        {"seed_candidate_id": "seed", "best_candidate_id": "best"},
    )
    write_json(
        root / "learning_receipt.json",
        {
            "inputs": protocol_inputs(root),
            "optimization_sha256": file_digest(root / "optimization.json"),
        },
    )
    freeze(root)
    return root


class FixtureExecutor:
    calls = 0

    def execute(self, target: Path, *args: object, **kwargs: object) -> ExecutionResult:
        self.calls += 1
        trial = CaseAttempt(
            result="fixture",
            tracelog={
                "protocol": "fixture-protocol",
                "agent_config_sha256": hashlib.sha256(
                    load_yaml_config(target / "config/agent.yaml", AgentConfig)
                    .model_dump_json()
                    .encode()
                ).hexdigest()
                if target.is_dir()
                else "fixture-base",
                "target_sha256": tree_digest(target)
                if target.is_dir()
                else file_digest(target),
                "status": "succeeded",
            },
            metadata=ExecutionMetadata(total_tokens=10, duration_seconds=1),
        )
        return ExecutionResult(
            cases=(
                CaseExecution(
                    id="one",
                    result=trial.result,
                    tracelog=trial.tracelog,
                    trials=(trial, trial, trial, trial),
                    metadata=ExecutionMetadata(total_tokens=40, duration_seconds=4),
                ),
            )
        )


class FixtureEvaluator:
    def evaluate(self, execution: ExecutionResult, *args: object) -> EvaluationResult:
        case = execution.cases[0]
        return EvaluationResult(
            total_score=0.5,
            cases=(
                CaseEvaluation(
                    id=case.id,
                    result=case.result,
                    tracelog={
                        "execution": case.model_dump(mode="json"),
                        "scores": [0, 1, 0, 1],
                    },
                    metadata=case.metadata,
                    expected=None,
                    score=0.5,
                    feedback="fixture",
                ),
            ),
        )


def test_four_arm_resume_report_and_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_experiment(tmp_path, monkeypatch)
    executor, evaluator = FixtureExecutor(), FixtureEvaluator()
    task = SimpleNamespace(
        executor=executor,
        evaluator=evaluator,
        datasets={DatasetRole.TEST: root / "task/datasets/test.json"},
    )
    monkeypatch.setattr(experiment, "load_task", lambda _: task)
    monkeypatch.setattr(
        experiment,
        "_baseline_execution",
        lambda *a: (executor.execute(root / "baseline.yaml"), evaluator),
    )
    experiment.evaluate(root, ["base", "yx", "prompt", "evolved"])
    assert executor.calls == 4
    experiment.evaluate(root, ["base", "yx", "prompt", "evolved"])
    assert executor.calls == 4
    report = build_report(root)
    assert report["primary_contrast"] == "evolved-yx"
    assert (root / "report.html").is_file()
    value = json.loads((root / "results/yx/evaluation.json").read_text())
    value["total_score"] = 1
    write_json(root / "results/yx/evaluation.json", value)
    with pytest.raises(ValueError, match="identity changed"):
        experiment.evaluate(root, ["yx"])
    with pytest.raises(ValueError, match="evidence"):
        build_report(root)


def test_freeze_rejects_modified_learning_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_experiment(tmp_path, monkeypatch)
    freeze(root)  # 实验组已生成后，恢复操作仍应保持幂等。
    write_json(
        root / "optimization.json",
        {"seed_candidate_id": "seed", "best_candidate_id": "seed"},
    )
    with pytest.raises(ValueError, match="Learning result"):
        freeze(root)


def test_frozen_model_environment_cannot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_experiment(tmp_path, monkeypatch)
    monkeypatch.setenv("FIXTURE_API_BASE", "https://changed.invalid")
    with pytest.raises(ValueError):
        experiment_state.verify_frozen(root)


def test_cross_arm_receipt_and_modified_execution_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_experiment(tmp_path, monkeypatch)
    executor, evaluator = FixtureExecutor(), FixtureEvaluator()
    task = SimpleNamespace(
        executor=executor,
        evaluator=evaluator,
        datasets={DatasetRole.TEST: root / "task/datasets/test.json"},
    )
    monkeypatch.setattr(experiment, "load_task", lambda _: task)
    experiment.evaluate(root, ["yx"])
    shutil.copytree(root / "results/yx", root / "results/prompt")
    with pytest.raises(ValueError, match="identity changed"):
        experiment.evaluate(root, ["prompt"])
    (root / "results/yx/receipt.json").unlink()
    value = json.loads((root / "results/yx/execution.json").read_text())
    value["cases"][0]["result"] = "changed after execution"
    write_json(root / "results/yx/execution.json", value)
    with pytest.raises(ValueError, match="Saved execution"):
        experiment.evaluate(root, ["yx"])


def test_status_includes_code_baseline_and_learning_model_is_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_experiment(tmp_path, monkeypatch)
    for archive in ("evidence", "results/base/raw", "smoke/base/raw"):
        write_json(
            root / archive / "trials/task/attempt.json",
            {
                "identity": {"task_id": archive},
                "status": "succeeded",
            },
        )
    assert len(experiment.status(root)["trials"]) == 3
    monkeypatch.setenv("EVOLUTION_LLM_MODEL", "openai/different-improver")
    with pytest.raises(ValueError, match="conditions changed"):
        experiment_state.verify_frozen(root)


def test_learning_restart_cannot_rebind_existing_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_experiment(tmp_path, monkeypatch)
    shutil.rmtree(root / "arms")
    write_json(root / "learning_started.json", {"inputs": protocol_inputs(root)})
    monkeypatch.setenv("EVOLUTION_LLM_MODEL", "openai/different-improver")
    # 重复预检查可以检查新依赖，但不能改写历史记录。
    write_json(root / "preflight.json", {"inputs": protocol_inputs(root)})
    with pytest.raises(ValueError, match="resume learning"):
        experiment.evolve(root)


@pytest.mark.parametrize("preset", ["pilot", "formal"])
def test_new_experiments_sample_once_without_changing_existing_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str
) -> None:
    existing = prepared_experiment(tmp_path, monkeypatch)
    frozen_task = (existing / "task/config.yaml").read_bytes()
    root = tmp_path / "single-trial"
    initialize(
        root,
        tmp_path / "template",
        tmp_path / "baseline.yaml",
        ExperimentConfig.model_validate({"preset": preset}),
    )
    settings = yaml.safe_load((root / "task/config.yaml").read_text())["settings"]
    assert [settings[f"{role}_trials"] for role in ("train", "validation", "test")] == [
        1,
        1,
        1,
    ]
    assert (existing / "task/config.yaml").read_bytes() == frozen_task


@pytest.mark.parametrize("enabled", [False, True])
def test_initialize_preserves_runtime_trace_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    prepared_experiment(tmp_path, monkeypatch)
    runtime = {
        "trace": {
            "enabled": enabled,
            "capture_content": False,
            "endpoint": "http://localhost:4318/v1/traces",
        }
    }
    (tmp_path / "template/runtime.yaml").write_text(yaml.safe_dump(runtime))
    root = tmp_path / "trace-config"
    initialize(
        root,
        tmp_path / "template",
        tmp_path / "baseline.yaml",
        ExperimentConfig(),
    )
    assert yaml.safe_load((root / "runtime.yaml").read_text()) == runtime


def test_uncollected_improver_usage_is_unknown_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_experiment(tmp_path, monkeypatch)
    costs = learning_cost(root)
    assert costs["improver_sessions"] is None
    assert costs["improver_sessions_without_usage"] is None
    assert costs["improver_known_tokens"] is None
    assert costs["improver_accounting_status"] == "not_collected"


@pytest.mark.parametrize("preset,expected", [("formal", 20), ("pilot", 2)])
@pytest.mark.parametrize("enabled,source", [(True, "best"), (False, "latest")])
def test_initialize_preserves_template_evolution_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
    expected: int,
    enabled: bool,
    source: str,
) -> None:
    existing = prepared_experiment(tmp_path, monkeypatch)
    frozen_task = (existing / "task/config.yaml").read_bytes()
    template = tmp_path / "template/config.yaml"
    raw = yaml.safe_load(template.read_text())
    raw.update(
        max_iterations=20,
        rollback_enabled=enabled,
        exploration_patience=3,
        rollback_changelog_source=source,
    )
    template.write_text(yaml.safe_dump(raw))
    root = tmp_path / "policy-experiment"
    initialize(
        root,
        template.parent,
        tmp_path / "baseline.yaml",
        ExperimentConfig.model_validate({"preset": preset}),
    )
    task = TaskConfig.model_validate(
        yaml.safe_load((root / "task/config.yaml").read_text())
    )
    assert task.max_iterations == expected
    assert task.rollback_enabled is enabled
    assert task.exploration_patience == 3
    assert task.rollback_changelog_source == source
    assert task.max_retries == 0
    assert [
        task.settings[f"{role}_trials"] for role in ("train", "validation", "test")
    ] == [1, 1, 1]
    assert (existing / "task/config.yaml").read_bytes() == frozen_task
