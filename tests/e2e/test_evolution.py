"""通用 Evolution 平台端到端测试。"""

import json
import re
from pathlib import Path

import pytest
from evolution.agent.plugins.evidence_tools import EvidenceTools
from evolution.agent.plugins.file_tools import WorkspaceTools
from evolution.candidates import CandidateRepository, collect_changes
from evolution.cli import _new_engine, run_cli
from evolution.diagnosis import build_diagnosis_brief
from evolution.engine import EvolutionEngine
from evolution.improver import ImprovementRequest
from evolution.models import DatasetRole, EvaluationResult, StopReason
from evolution.task import load_task, load_task_config


class _DeterministicImprover:
    """通过真实文件工具交付修改、继承历史及逐 case 回填。"""

    def __init__(self, scores: tuple[str | None, ...] = ("0.4", "0.8", None)) -> None:
        self.scores = scores
        self.requests: list[ImprovementRequest] = []
        self.inputs: list[str] = []
        self.outputs: list[str] = []

    async def improve(self, request: ImprovementRequest) -> None:
        self.requests.append(request)
        files = WorkspaceTools()
        files.bind(request)
        evidence = EvidenceTools()
        evidence.bind(request)
        log_path = str(request.changelog_path)
        text = request.changelog_path.read_text()
        self.inputs.append(text)
        groups = re.split(r"(?m)(?=^## 评测版本：)", text)
        matched = [
            group
            for group in groups
            if f"## 迭代版本：{request.evidence_candidate_id}" in group.splitlines()
        ]
        if len(matched) == 1:
            markers = re.findall(r"^CHANGE_RESULT_.*$", matched[0], re.M)
            for marker, case_id in zip(
                markers[-2:], ("train-1", "train-2"), strict=False
            ):
                case = json.loads(evidence.read_case_evidence(case_id).content)
                evidence.read_case_tracelog(case_id)
                assert text.count(marker) == 1
                assert "matches=1" in files.search_path(log_path, marker).content
                files.write_file(
                    "replace",
                    log_path,
                    f"{case_id}: score={case['score']}，Train 证据版本："
                    f"{request.evidence_candidate_id}；联合修改效果，未隔离单项贡献。",
                    marker,
                    False,
                )
                assert marker not in files.read_file(log_path, limit=1000).content
        score = self.scores[len(self.requests) - 1]
        if score is not None:
            _write_changes(files, request, score, len(self.requests))
        else:
            files.write_file(
                "prepend",
                log_path,
                f"\n## 评测版本：{request.base_candidate_id}\n"
                f"## 迭代版本：{request.output_candidate_id}\n"
                "### 问题类型\n证据不足以支持进一步修改。\n"
                "### 问题Case\n无具体 case\n### 文件变更\n未修改\n"
                "### 变更依据\n没有合理的新修改。\n"
                "### 相关CASE测试结果\n未实施：NO_CHANGE。\n"
                "### Unresolved Issues\n无\n### Suggestions\n等待新证据。\n",
            )
        self.outputs.append(request.changelog_path.read_text())
        files.unbind()
        evidence.unbind()


def _write_changes(
    files: WorkspaceTools,
    request: ImprovementRequest,
    score: str,
    iteration: int,
) -> None:
    """写两组独立文件 diff 和标记，结果在未来轮次回填。"""
    log_path = str(request.changelog_path)
    sections = [
        f"\n## 评测版本：{request.base_candidate_id}\n"
        f"## 迭代版本：{request.output_candidate_id}\n",
    ]
    for index, (path, value) in enumerate(
        (("score.txt", score), ("note.txt", f"iteration {iteration}")),
        start=1,
    ):
        target = request.workspace_dir / path
        old = target.read_text() if target.exists() else ""
        if old == value:
            continue
        files.write_file("overwrite" if target.exists() else "create", path, value)
        sections.append(
            f"### 问题类型\n测试问题 {index}，改善评分。\n"
            f"### 问题Case\ntrain-{index}\n"
            f"### 文件变更\n```diff\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n-{old}\n+{value}\n```\n"
            "### 变更依据\nTrain 证据提示改善，保留已有行为。\n"
            f"### 相关CASE测试结果\nCHANGE_RESULT_{request.output_candidate_id}"
            f"_{iteration}_{index}\n",
        )
    sections.append("### Unresolved Issues\n无\n### Suggestions\n无\n\n")
    files.write_file("prepend", log_path, "".join(sections))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["best", "latest"])
async def test_configured_rollback_end_to_end(
    tmp_path: Path, enabled: bool, mode: str
) -> None:
    """真实配置、任务加载、文件工具、恢复和证据绑定贯通。"""
    root = _write_task(tmp_path)
    config_path = root / "config.yaml"
    config_path.write_text(
        config_path.read_text() + f"rollback_enabled: {str(enabled).lower()}\n"
        f"exploration_patience: 2\nrollback_changelog_source: {mode}\n"
    )
    (root / "evaluator.py").write_text(
        _EVALUATOR_SOURCE.replace("del dataset_path", "").replace(
            "score=float(case.result)",
            'score=(1 - float(case.result) if dataset_path.stem == "validation" '
            "else float(case.result))",
        )
    )
    config = load_task_config(root)
    repository = CandidateRepository(
        root,
        workspace_root=tmp_path / ".evolution",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=(),
    )
    improver = _DeterministicImprover(("0.6", "0.7", "0.8"))
    result = await _new_engine(
        repository=repository, task=load_task(config), improver=improver, config=config
    ).run()
    candidates = repository.list_candidates()
    seed = candidates[0]
    assert result.best_candidate_id == seed.id
    assert result.iterations_attempted == 3
    assert result.candidates_created == 3 + int(enabled)
    request = improver.requests[-1]
    assert request.rollback_changelog_source == (mode if enabled else None)
    assert request.evidence_reused_from_candidate_id == (seed.id if enabled else None)
    final_log = candidates[-1].changelog_path.read_text()
    assert final_log.lstrip().startswith(f"## 评测版本：{candidates[-2].id}")
    if enabled:
        restored = candidates[-2]
        assert repository.load_rollback(restored.id) is not None
        assert (restored.target_dir / "score.txt").read_text() == "0.5"
        assert not (restored.directory / "test").exists()
        assert ("CHANGE_RESULT_" in restored.changelog_path.read_text()) == (
            mode == "latest"
        )
        files = EvidenceTools()
        files.bind(request)
        assert json.loads(files.read_case_evidence("train-1").content)["score"] == 0.5
        assert "0.5" in files.read_case_tracelog("train-1").content
        files.unbind()


def test_manual_test_cli_without_agent_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实 CLI 加载假场景，只写指定候选 Test，且终端不输出轨迹。"""
    root = _write_task(tmp_path)
    config = load_task_config(root)
    repository = CandidateRepository(
        root,
        workspace_root=tmp_path / ".evolution",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=config.workspace.read_only,
        inaccessible=config.workspace.inaccessible,
    )
    seed = repository.initialize_seed()
    assert not (tmp_path / "config/runtime.yaml").exists()

    exit_code = run_cli(["test", str(root), seed.id], project_root=tmp_path, environ={})

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["total_score"] == 0.5
    assert all("tracelog" not in case for case in output["cases"])
    saved = repository.load_evaluation(seed.id, DatasetRole.TEST)
    assert saved is not None and saved.cases[0].tracelog
    assert repository.load_execution(seed.id, DatasetRole.TRAIN) is None
    assert repository.load_execution(seed.id, DatasetRole.VALIDATION) is None
    assert repository.list_candidates() == (seed,)

    execution_path = seed.directory / "test/execution.json"
    execution = execution_path.read_bytes()
    modified_at = execution_path.stat().st_mtime_ns
    (seed.directory / "test/evaluation.json").unlink()
    assert (
        run_cli(
            ["test", str(root), seed.id, "--reevaluate"],
            project_root=tmp_path,
            environ={},
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["total_score"] == 0.5
    assert execution_path.read_bytes() == execution
    assert execution_path.stat().st_mtime_ns == modified_at
    assert repository.load_evaluation(seed.id, DatasetRole.TEST) is not None


async def test_generic_optimization_preserves_complete_fact_graph(
    tmp_path: Path,
) -> None:
    """固定 Scenario 应覆盖连续迭代、验证选优与停止的完整事实。"""
    task_root = _write_task(tmp_path)
    config = load_task_config(task_root)
    task = load_task(config)
    repository = CandidateRepository(
        config.root,
        workspace_root=tmp_path / ".evolution",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=config.workspace.read_only,
        inaccessible=config.workspace.inaccessible,
    )
    improver = _DeterministicImprover()
    engine = EvolutionEngine(
        repository=repository,
        task=task,
        improver=improver,
        objective=config.objective,
        read_only=config.workspace.read_only,
        inaccessible=config.workspace.inaccessible,
        max_iterations=config.max_iterations,
        max_retries=config.max_retries,
        retry_delay_seconds=config.retry_delay_seconds,
    )

    result = await engine.run()

    candidates = repository.list_candidates()
    seed, rejected, accepted = candidates
    assert result.seed_candidate_id == seed.id
    assert result.best_candidate_id == accepted.id
    assert result.stop_reason is StopReason.ITERATION_LIMIT
    assert result.iterations_attempted == 3
    assert result.candidates_created == 2
    assert result.changelog_paths == (rejected.changelog_path, accepted.changelog_path)
    assert rejected.parent_id == seed.id
    assert accepted.parent_id == rejected.id
    assert {c.path for c in collect_changes(seed.target_dir, accepted.target_dir)} == {
        "score.txt",
        "note.txt",
    }
    assert improver.inputs == [
        "",
        rejected.changelog_path.read_text(),
        accepted.changelog_path.read_text(),
    ]
    assert "train-1: score=0.4" in accepted.changelog_path.read_text()
    assert f"CHANGE_RESULT_{accepted.id}_1_1" not in accepted.changelog_path.read_text()
    assert "train-1: score=0.8" in improver.outputs[2]
    assert "未实施：NO_CHANGE" in improver.outputs[2]
    assert "未实施" not in accepted.changelog_path.read_text()
    assert seed.changelog_path.read_text() == ""
    assert all(not r.workspace_dir.parent.exists() for r in improver.requests)
    for candidate in candidates:
        assert not (candidate.directory / "reviews").exists()
        assert not (candidate.directory / "change.log").exists()
        assert not (candidate.target_dir / "changelog.md").exists()
    assert repository.load_evaluation(seed.id, DatasetRole.TRAIN) is not None
    assert repository.load_evaluation(seed.id, DatasetRole.VALIDATION) is not None
    rejected_train = repository.load_evaluation(rejected.id, DatasetRole.TRAIN)
    assert rejected_train is not None
    assert rejected_train.parent_comparison is not None
    assert rejected_train.parent_comparison.total_score_delta == pytest.approx(-0.1)
    assert repository.load_execution(rejected.id, DatasetRole.VALIDATION) is None
    accepted_train = repository.load_evaluation(accepted.id, DatasetRole.TRAIN)
    assert accepted_train is not None
    assert accepted_train.parent_comparison is not None
    assert accepted_train.parent_comparison.previous_candidate_id == rejected.id
    assert accepted_train.cases[0].previous_score == 0.4
    assert repository.load_evaluation(accepted.id, DatasetRole.VALIDATION) is not None
    for request in improver.requests:
        assert request.train_evaluation.cases
        assert all(
            case.id.startswith("train-") for case in request.train_evaluation.cases
        )
    assert all(
        repository.load_execution(candidate.id, DatasetRole.TEST) is None
        for candidate in candidates
    )


async def test_five_generations_inherit_history_and_backfill_only_child(
    tmp_path: Path,
) -> None:
    """第五轮持有前四轮记录，旧版本结果快照不被回填覆盖。"""
    root = _write_task(tmp_path)
    (root / "target/score.txt").write_text("0.1")
    config = load_task_config(root)
    repository = CandidateRepository(
        root,
        workspace_root=tmp_path / ".evolution",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=(),
    )
    improver = _DeterministicImprover(("0.2", "0.3", "0.4", "0.5", "0.6"))
    engine = EvolutionEngine(
        repository=repository,
        task=load_task(config),
        improver=improver,
        objective=config.objective,
        read_only=(),
        max_iterations=5,
        max_retries=0,
        retry_delay_seconds=0,
    )
    result = await engine.run()
    candidates = repository.list_candidates()
    assert result.candidates_created == 5
    assert len(result.changelog_paths) == 5
    assert improver.inputs[4].count("## 评测版本：") == 4
    for index, candidate in enumerate(candidates[1:], start=1):
        text = candidate.changelog_path.read_text()
        assert candidate.parent_id == candidates[index - 1].id
        assert text.count("## 评测版本：") == index
        assert text.count("### 问题类型") == index * 2
        assert text.count("### Unresolved Issues") == index
        assert text.count("### Suggestions") == index
        assert len(re.findall(r"^CHANGE_RESULT_.*$", text, re.M)) == 2
        assert f"EVALUATION_COMPARISON_{candidate.id}" not in text
        assert f"ADOPTION_RESULT_{candidate.id}" not in text
        if index < len(improver.inputs):
            assert text == improver.inputs[index]
        if index > 1:
            assert f"train-1: score={improver.scores[index - 2]}" in text
            assert f"train-2: score={improver.scores[index - 2]}" in text
    # 重新打开旧实验：历史缺失创建空文件，保留评测与 Target，不补造历史。
    seed = candidates[0]
    seed.changelog_path.unlink()
    before = repository.load_evaluation(seed.id, DatasetRole.TRAIN)
    assert repository.initialize_seed().changelog_path.read_text() == ""
    assert repository.load_evaluation(seed.id, DatasetRole.TRAIN) == before
    latest = candidates[-1].changelog_path.read_text()
    assert latest.count("## 评测版本：") == 5
    assert f"EVALUATION_COMPARISON_{candidates[-1].id}" not in latest
    assert f"ADOPTION_RESULT_{candidates[-1].id}" not in latest


async def test_restarted_run_recovers_evaluation_before_inheriting_failed_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评分中断后重新打开磁盘仓库，恢复最新候选并继续优化。"""
    root = _write_task(tmp_path)
    config = load_task_config(root)
    task = load_task(config)

    def repository() -> CandidateRepository:
        return CandidateRepository(
            root,
            workspace_root=tmp_path / ".evolution",
            initial_target=config.initial_target,
            objective=config.objective,
            read_only=(),
        )

    def engine(
        repo: CandidateRepository, improver: _DeterministicImprover
    ) -> EvolutionEngine:
        return EvolutionEngine(
            repository=repo,
            task=task,
            improver=improver,
            objective=config.objective,
            read_only=(),
            max_iterations=1,
            max_retries=0,
            retry_delay_seconds=0,
        )

    repo = repository()
    first = _DeterministicImprover(("0.4",))
    original = repo.save_evaluation

    def fail_child(
        candidate_id: str, role: DatasetRole, evaluation: EvaluationResult
    ) -> None:
        if repo.load_candidate(candidate_id).parent_id is not None:
            raise OSError("evaluation save interrupted")
        original(candidate_id, role, evaluation)

    with monkeypatch.context() as patch:
        patch.setattr(repo, "save_evaluation", fail_child)
        with pytest.raises(OSError, match="interrupted"):
            await engine(repo, first).run()
    seed, rejected = repo.list_candidates()
    execution = rejected.directory / "train/execution.json"
    before = execution.read_bytes()
    second = _DeterministicImprover(("0.8",))
    reopened = repository()
    result = await engine(reopened, second).run()
    request = second.requests[0]
    assert request.base_candidate_id == rejected.id
    assert request.evidence_candidate_id == rejected.id
    assert request.changelog_source_candidate_id == rejected.id
    assert execution.read_bytes() == before
    assert request.train_evaluation.total_score == 0.4
    assert result.iterations_attempted == result.candidates_created == 1
    newest = reopened.list_candidates()[-1]
    assert newest.parent_id == rejected.id
    assert newest.changelog_path.read_text().count("## 迭代版本：") == 2
    assert "train-1: score=0.4" in newest.changelog_path.read_text()


def _write_task(root: Path) -> Path:
    task = root / "task"
    target = task / "target"
    datasets = task / "datasets"
    target.mkdir(parents=True)
    datasets.mkdir(parents=True)
    (target / "score.txt").write_text("0.5", encoding="utf-8")
    (task / "config.yaml").write_text(
        """objective: improve the score
initial_target: target
workspace: {read_only: [], inaccessible: []}
max_iterations: 3
""",
        encoding="utf-8",
    )
    for role, case_ids in {
        "train": ("train-1", "train-2"),
        "validation": ("validation-1",),
        "test": ("test-1",),
    }.items():
        (datasets / f"{role}.json").write_text(
            json.dumps([{"id": case_id} for case_id in case_ids]),
            encoding="utf-8",
        )
    (task / "executor.py").write_text(_EXECUTOR_SOURCE, encoding="utf-8")
    (task / "evaluator.py").write_text(_EVALUATOR_SOURCE, encoding="utf-8")
    return task


_EXECUTOR_SOURCE = """
import json
import re

from evolution.models import CaseExecution, ExecutionMetadata, ExecutionResult


class FakeExecutor:
    def execute(self, candidate_dir, dataset_path, role, *,
                max_case_concurrency=1, max_retries=3,
                retry_delay_seconds=2, cancel_event=None):
        del role
        score = (candidate_dir / "score.txt").read_text(encoding="utf-8").strip()
        cases = json.loads(dataset_path.read_text(encoding="utf-8"))
        return ExecutionResult(cases=tuple(
            CaseExecution(
                id=case["id"],
                result=score,
                tracelog={"candidate_score": score},
                metadata=ExecutionMetadata(total_tokens=1, duration_seconds=0),
            )
            for case in cases
        ))


def create_executor(task_dir, settings):
    del task_dir, settings
    return FakeExecutor()
"""


_EVALUATOR_SOURCE = """
from evolution.models import CaseEvaluation, EvaluationResult


class FakeEvaluator:
    def evaluate(self, execution, dataset_path):
        del dataset_path
        cases = tuple(
            CaseEvaluation(
                id=case.id,
                result=case.result,
                tracelog=case.tracelog,
                metadata=case.metadata,
                expected={"score": 1.0},
                score=float(case.result),
                feedback="increase score",
            )
            for case in execution.cases
        )
        return EvaluationResult(
            total_score=sum(case.score for case in cases) / len(cases),
            cases=cases,
        )


def create_evaluator(task_dir, settings):
    del task_dir, settings
    return FakeEvaluator()
"""


async def test_optimizer_retry_publishes_only_successful_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前三次失败后的成功修改才进入通用评测及正式 Candidate。"""
    config = load_task_config(_write_task(tmp_path))
    repository = CandidateRepository(
        config.root,
        workspace_root=tmp_path / ".evolution",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=(),
    )
    requests: list[ImprovementRequest] = []

    class FlakyImprover:
        async def improve(self, request: ImprovementRequest) -> None:
            requests.append(request)
            assert (request.workspace_dir / "score.txt").read_text() == "0.5"
            (request.workspace_dir / "score.txt").write_text("1.0")
            if len(requests) < 4:
                raise RuntimeError("temporary failure")
            files = WorkspaceTools()
            files.bind(request)
            # score 已由本次尝试修改，只追加对应记录以通过发布检查。
            files.write_file(
                "append",
                str(request.changelog_path),
                f"## 评测版本：{request.base_candidate_id}\n"
                f"## 迭代版本：{request.output_candidate_id}\n"
                "### 问题类型\n改善评分。\n",
            )
            files.unbind()

    async def sleep(delay: float) -> None:
        assert delay == 2

    monkeypatch.setattr("evolution.engine.asyncio.sleep", sleep)
    result = await EvolutionEngine(
        repository=repository,
        task=load_task(config),
        improver=FlakyImprover(),
        objective=config.objective,
        read_only=(),
        max_iterations=1,
        max_retries=3,
        retry_delay_seconds=0,
    ).run()
    assert result.validation_score == 1
    assert result.candidates_created == 1
    assert result.iterations_attempted == 1
    assert len(result.changelog_paths) == 1
    assert len(repository.list_candidates()) == 2


async def test_csv_scenario_retries_and_exposes_saved_evidence(tmp_path: Path) -> None:
    """平台不解析数据内容；CSV 场景完成执行、重试、评分与证据读取。"""
    root = _write_task(tmp_path)
    config_file = root / "config.yaml"
    config_file.write_text(
        config_file.read_text()
        + "datasets: {train: train.csv, validation: validation.csv, test: test.csv}\n"
    )
    for role in DatasetRole:
        # 跨 Role 同名 ID 合法，由场景负责定义业务隔离。
        (root / f"{role.value}.csv").write_text("case_key,value\na,1\nb,2\n")
    (root / "executor.py").write_text(_CSV_EXECUTOR_SOURCE)
    (root / "evaluator.py").write_text(_CSV_EVALUATOR_SOURCE)
    config = load_task_config(root)
    task = load_task(config)
    repository = CandidateRepository(
        root,
        workspace_root=tmp_path / ".evolution",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=(),
    )
    engine = EvolutionEngine(
        repository=repository,
        task=task,
        improver=_DeterministicImprover(),
        objective=config.objective,
        read_only=(),
        max_iterations=0,
        max_retries=1,
        max_case_concurrency=2,
        retry_delay_seconds=0,
    )
    outcome = await engine.run()
    assert (
        repository.load_execution(outcome.seed_candidate_id, DatasetRole.TEST) is None
    )
    for role in (DatasetRole.TRAIN, DatasetRole.VALIDATION):
        execution = repository.load_execution(outcome.seed_candidate_id, role)
        assert execution is not None
        assert [case.id for case in execution.cases] == ["a", "b"]
        assert [len(case.attempts) for case in execution.cases] == [2, 0]
        assert execution.cases[0].metadata.total_tokens == 2
    evaluation = repository.load_evaluation(
        outcome.seed_candidate_id, DatasetRole.TRAIN
    )
    assert evaluation is not None and evaluation.total_score == 1
    tools = EvidenceTools()
    tools.bind(
        ImprovementRequest(
            objective="test",
            base_candidate_id=outcome.seed_candidate_id,
            changelog_source_candidate_id=outcome.seed_candidate_id,
            evidence_candidate_id=outcome.seed_candidate_id,
            output_candidate_id="candidate-output",
            evidence_parent_candidate_id=None,
            workspace_dir=root / "workspace",
            read_only=(),
            train_evaluation=evaluation,
            diagnosis_brief=build_diagnosis_brief(
                outcome.seed_candidate_id, evaluation
            ),
            changelog_path=root / "changelog.md",
        )
    )
    assert json.loads(tools.read_case_evidence("a").content)["expected"] == "1"
    assert "csv value 1" in tools.read_case_tracelog("a").content
    (root / "train.csv").write_text("case_key,value\na,3\nb,2\n")
    assert load_task(load_task_config(root)).digest != task.digest


_CSV_EXECUTOR_SOURCE = """
import csv
from evolution.execution import run_cases
from evolution.models import CaseExecution, ExecutionMetadata

class Executor:
    def execute(self, target, dataset, role, *, max_case_concurrency=1,
                max_retries=3, retry_delay_seconds=2, cancel_event=None):
        with dataset.open() as source:
            cases = [(row["case_key"], row["value"]) for row in csv.DictReader(source)]
        attempts = {}
        def execute_once(case, stop):
            key, value = case
            attempts[key] = attempts.get(key, 0) + 1
            return CaseExecution(id=key, result=value, tracelog=f"csv value {value}",
                retryable=key == "a" and attempts[key] == 1,
                metadata=ExecutionMetadata(total_tokens=1, duration_seconds=1))
        return run_cases(cases, case_id=lambda case: case[0], execute_once=execute_once,
                         max_case_concurrency=max_case_concurrency,
                         max_retries=max_retries,
                         retry_delay_seconds=retry_delay_seconds,
                         cancel_event=cancel_event)

def create_executor(task_dir, settings):
    return Executor()
"""

_CSV_EVALUATOR_SOURCE = """
import csv
from evolution.models import CaseEvaluation, EvaluationResult

class Evaluator:
    def evaluate(self, execution, dataset):
        with dataset.open() as source:
            expected = {row["case_key"]: row["value"] for row in csv.DictReader(source)}
        assert set(expected) == {case.id for case in execution.cases}
        cases = tuple(CaseEvaluation(id=case.id, result=case.result,
            tracelog=case.tracelog, metadata=case.metadata,
            expected=expected[case.id], score=float(case.result == expected[case.id]),
            feedback="") for case in execution.cases)
        return EvaluationResult(
            total_score=sum(c.score for c in cases) / len(cases), cases=cases)

def create_evaluator(task_dir, settings):
    return Evaluator()
"""
