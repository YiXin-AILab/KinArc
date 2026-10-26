"""EvoAgentBench 集成协议的行为测试。"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest
from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from benchmarks.evoagentbench.common.identity import bind_manifest
from benchmarks.evoagentbench.common.process import invoke, remove_owned_container
from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.data.prepare_dataset import split_ids
from benchmarks.evoagentbench.domains import domain_adapter, domain_worker
from benchmarks.evoagentbench.domains.domain_settings import (
    DomainSettings,
    settings_for,
)
from benchmarks.evoagentbench.reporting.report import compare, metrics
from benchmarks.evoagentbench.runners.tools import safe_path, state_tools
from evolution.models import (
    CaseAttempt,
    CaseEvaluation,
    CaseExecution,
    DatasetRole,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
)


def paired_result(scores: list[float], protocol: str = "same") -> EvaluationResult:
    """构造具有不可变执行身份的完整报告证据。"""
    trials = tuple(
        attempt().model_copy(
            update={
                "tracelog": {
                    "status": "succeeded",
                    "protocol": protocol,
                    "agent_config_sha256": "config",
                    "target_sha256": "target",
                }
            }
        )
        for _ in scores
    )
    execution = CaseExecution(
        id="a",
        result=trials[0].result,
        tracelog=trials[0].tracelog,
        trials=trials,
        metadata=ExecutionMetadata(
            total_tokens=10 * len(scores), duration_seconds=len(scores)
        ),
    )
    case = CaseEvaluation(
        id="a",
        result=execution.result,
        metadata=execution.metadata,
        tracelog={"scores": scores, "execution": execution.model_dump(mode="json")},
        expected=None,
        score=sum(scores) / len(scores),
        feedback="test",
    )
    return EvaluationResult(total_score=case.score, cases=(case,))


def test_paired_report_rejects_different_protocols() -> None:
    with pytest.raises(ValueError, match="identities differ"):
        compare(paired_result([0, 0]), paired_result([1, 1], "different"))


def test_paired_report_counts_improvement_and_cost() -> None:
    result = compare(
        paired_result([0, 0]), paired_result([1, 0]), bootstrap_samples=100
    )
    assert result["delta_pass@1"] == 0.5
    assert result["improved"] == 1
    assert result["baseline_tokens"] == 20


def test_venv_python_path_keeps_symlink(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    interpreter.symlink_to(sys.executable)
    runtime = tmp_path / "runtime.yaml"
    runtime.touch()
    config = settings(tmp_path).model_copy(update={"upstream_python": interpreter})
    resolved = settings_for(tmp_path, config.model_dump(mode="json"))
    assert resolved.upstream_python == interpreter


@pytest.mark.parametrize("trials", [1, 4])
def test_executor_honors_trial_count_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trials: int
) -> None:
    config = settings(tmp_path)
    if trials != 1:
        config = config.model_copy(update={"test_trials": trials})
    monkeypatch.setattr(domain_adapter, "experiment_identity", lambda *_: "protocol")
    calls = []

    def worker(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(args)
        return attempt().model_dump(mode="json")

    monkeypatch.setattr(domain_adapter, "invoke", worker)
    target = tmp_path / "candidate"
    target.mkdir()
    result = domain_adapter.DomainExecutor(config).execute(
        target,
        manifest(tmp_path, config),
        DatasetRole.TEST,
        max_retries=0,
    )
    assert len(calls) == trials
    assert (len(result.cases[0].trials) or 1) == trials
    assert result.cases[0].metadata.total_tokens == 10 * trials


def settings(tmp_path: Path) -> DomainSettings:
    """直接构造显式配置，不读取部署配置。"""
    return DomainSettings(
        domain="information_retrieval",
        upstream_repo=tmp_path,
        upstream_python=Path(sys.executable),
        domain_config=tmp_path / "domain.yaml",
        runtime_config=tmp_path / "runtime.yaml",
    )


def test_execution_image_is_pinned_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = settings(tmp_path).model_copy(update={"domain": "knowledge_work"})
    image_id = "sha256:" + "a" * 64
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: image_id)
    resolved = settings_for(tmp_path, config.model_dump(mode="json"))
    executor = domain_adapter.DomainExecutor(resolved)
    assert executor.settings.execution_image == image_id
    assert config.execution_image == "ea-bench-office:local"


def test_parent_cleanup_failure_is_not_silenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "daemon unavailable"),
    )
    with pytest.raises(RuntimeError, match="Failed to remove"):
        remove_owned_container("owned-container")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 1, "", "No such container: gone"
        ),
    )
    remove_owned_container("gone")


def manifest(tmp_path: Path, config: DomainSettings) -> Path:
    """写入一个公开测试任务。"""
    path = tmp_path / "test.json"
    path.write_text(
        Dataset(
            role="test",
            upstream_revision=config.upstream_revision,
            source_sha256="source",
            cases=(Case(id="a", prompt="solve"),),
        ).model_dump_json()
    )
    return path


def attempt(response: str = "answer") -> CaseAttempt:
    """一次成功的 Runtime 执行完成，其评分结果单独处理。"""
    return CaseAttempt(
        result=response,
        tracelog={"status": "succeeded", "protocol": "protocol"},
        metadata=ExecutionMetadata(total_tokens=10, duration_seconds=1),
    )


def test_split_never_uses_test_for_validation() -> None:
    raw = {"train": ["a", "b", "c", "d"], "test": ["x", "y"]}
    result = split_ids(raw, 2, 42)
    assert result == split_ids(raw, 2, 42)
    assert set(result["train"]) | set(result["validation"]) == set(raw["train"])
    assert not set(result["train"]) & set(result["validation"])
    assert result["test"] == raw["test"]


@pytest.mark.parametrize(
    "raw",
    [
        {"train": ["a", "a"], "test": ["x"]},
        {"train": ["a", "b"], "test": ["a"]},
    ],
)
def test_invalid_official_split_rejected(raw: dict[str, list[str]]) -> None:
    with pytest.raises(ValueError):
        split_ids(raw, 1, 42)


def test_reliability_estimators_are_not_power_of_mean() -> None:
    actual = metrics({"a": [1, 1, 0, 0], "b": [1, 1, 1, 1]})
    assert actual["pass@1"] == 0.75
    assert actual["pass@2"] == pytest.approx(11 / 12)
    assert actual["pass^2"] == pytest.approx(7 / 12)
    assert actual["pass^4"] == 0.5
    assert actual["pass@4"] == 1


@pytest.mark.parametrize("session_jsonl", [None, '{"role":"assistant"}\n'])
def test_evaluator_scores_all_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_jsonl: str | None
) -> None:
    config = settings(tmp_path).model_copy(update={"test_trials": 4})
    trials = tuple(attempt(str(i)) for i in range(4))
    for trial in trials:
        assert isinstance(trial.tracelog, dict)
        score = 1.0 if trial.result in {"1", "3"} else 0.0
        trial.tracelog["verifier"] = {
            "score": score,
            "passed": score > 0,
            "pass_threshold": 0.0,
        }
        if session_jsonl is not None:
            trial.tracelog["session_jsonl"] = session_jsonl
    execution = ExecutionResult(
        cases=(
            CaseExecution(
                id="a",
                result="0",
                tracelog=trials[0].tracelog,
                metadata=ExecutionMetadata(total_tokens=40, duration_seconds=4),
                trials=trials,
            ),
        )
    )
    monkeypatch.setattr(domain_adapter, "experiment_identity", lambda *_: "protocol")
    result = domain_adapter.DomainEvaluator(config).evaluate(
        execution,
        manifest(tmp_path, config),
    )
    assert result.total_score == 0.5
    assert isinstance(result.cases[0].tracelog, dict)
    assert result.cases[0].tracelog["scores"] == [0.0, 1.0, 0.0, 1.0]
    assert result.cases[0].expected is None
    assert result.cases[0].metadata.total_tokens == 40


def test_missing_case_is_not_silently_dropped(tmp_path: Path) -> None:
    config = settings(tmp_path)
    with pytest.raises(ValueError, match="exactly cover the dataset"):
        domain_adapter.DomainEvaluator(config).evaluate(
            ExecutionResult(cases=()),
            manifest(tmp_path, config),
        )


def test_retry_policy_rejected_before_dispatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        domain_adapter.DomainExecutor(settings(tmp_path)).execute(
            tmp_path,
            tmp_path / "absent",
            DatasetRole.TEST,
        )


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../test.json", "config/agent.yaml", "skills/../../test.json"],
)
def test_learned_state_cannot_read_grader(tmp_path: Path, path: str) -> None:
    with pytest.raises(ToolError):
        safe_path(tmp_path, path)


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "skills").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ToolError):
        safe_path(root, "skills/answer")


async def test_scratch_does_not_modify_frozen_memory(tmp_path: Path) -> None:
    target, scratch = tmp_path / "target", tmp_path / "scratch"
    (target / "memory").mkdir(parents=True)
    scratch.mkdir()
    (target / "memory/index.md").write_text("frozen")
    (target / "tools.json").write_text(
        json.dumps(
            {
                "read_description": "read",
                "write_description": "write",
            }
        )
    )
    read, write = state_tools(target, scratch)
    ctx = SessionContext(app_id="a", agent_id="a", user_id="u", session_id="s")
    await write.handler.invoke(
        ctx=ctx, arguments={"path": "memory/index.md", "content": "scratch"}
    )
    output = await read.handler.invoke(ctx=ctx, arguments={"path": "memory/index.md"})
    assert output.content == "scratch"
    assert (target / "memory/index.md").read_text() == "frozen"


def test_subprocess_deadline_and_pre_cancel() -> None:
    with pytest.raises(TimeoutError):
        invoke([sys.executable, "-c", "import time; time.sleep(30)"], {}, timeout=0.1)
    canceled = Event()
    canceled.set()
    with pytest.raises(asyncio.CancelledError):
        invoke(
            [sys.executable, "-c", "raise AssertionError"],
            {},
            timeout=1,
            cancel=canceled,
        )


def test_worker_file_contract(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys,json\nfrom pathlib import Path\n"
        "request=json.loads(Path(sys.argv[2]).read_text())\n"
        "Path(sys.argv[4]).write_text(json.dumps({'value':request['value']}))\n"
    )
    assert invoke([sys.executable, str(worker)], {"value": 7}, timeout=5) == {
        "value": 7
    }


def test_resume_rejects_changed_protocol(tmp_path: Path) -> None:
    path = tmp_path / "manifest"
    bind_manifest(path, "a")
    bind_manifest(path, "a")
    with pytest.raises(ValueError, match="protocol changed"):
        bind_manifest(path, "b")
    assert path.read_text().strip() == "a"


@pytest.mark.parametrize("reward", [0, 1])
def test_verifier_boundary_removes_hidden_feedback(reward: int) -> None:
    result = domain_worker.normalized_verdict(
        {
            "reward": reward,
            "metadata": {"expected": "secret answer"},
        }
    )
    assert result["score"] == reward
    assert "secret answer" not in json.dumps(result)
