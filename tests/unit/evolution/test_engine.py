"""Evolution 守擂编排测试。"""

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from evolution.candidates import CandidateRepository
from evolution.engine import EvolutionEngine
from evolution.execution import compare_evaluations, run_cases
from evolution.improver import ImprovementRequest
from evolution.models import (
    CaseEvaluation,
    CaseExecution,
    DatasetRole,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
    RollbackChangelogSource,
    StopReason,
)
from evolution.task import TaskBundle


@pytest.mark.parametrize("resume", [False, True])
async def test_run_logs_candidate_and_stage_progress(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, resume: bool
) -> None:
    """日志区分启动、阶段恢复、优化和结束，复用执行不伪装成重新执行。"""
    engine, repository, executor, improver = _engine(
        tmp_path, scores=_base_scores(), changes=[None], max_iterations=1
    )
    if resume:
        seed = repository.initialize_seed()
        repository.save_execution(
            seed.id,
            DatasetRole.TRAIN,
            executor.execute(seed.target_dir, tmp_path, DatasetRole.TRAIN),
        )
    with caplog.at_level("INFO", logger="evolution.engine"):
        await engine.run()
    seed = repository.initialize_seed()
    messages = [r.getMessage() for r in caplog.records if r.name == "evolution.engine"]
    mode = "resume" if resume else "initialize"
    assert messages[0] == f"run_started mode={mode} max_iterations=1"
    assert f"recovery_started seed={seed.id} latest={seed.id} candidates=1" in messages
    assert (
        f"candidate_started candidate={seed.id} parent=none stage=assessment"
        in messages
    )
    execution_event = "stage_reused" if resume else "stage_resuming"
    assert any(
        m.startswith(
            f"{execution_event} candidate={seed.id} role=train stage=execution"
        )
        for m in messages
    )
    evaluation_start = next(
        i
        for i, m in enumerate(messages)
        if m.startswith(
            f"stage_resuming candidate={seed.id} role=train stage=evaluation"
        )
    )
    evaluation_end = next(
        i
        for i, m in enumerate(messages)
        if m.startswith(
            f"stage_completed candidate={seed.id} role=train stage=evaluation"
        )
    )
    attempt = next(
        i for i, m in enumerate(messages) if m.startswith("improver_attempt_started")
    )
    assert evaluation_start < evaluation_end < attempt
    assert f"candidate={seed.id} stage=evolution_agent" in messages[attempt]
    assert (
        f"output_candidate={improver.requests[0].output_candidate_id}"
        in messages[attempt]
    )
    assert any(
        m.startswith("candidate_not_published") and "reason=no_change" in m
        for m in messages
    )
    assert messages[-1].startswith(f"run_completed best_candidate={seed.id}")
    assert "stop_reason=iteration_limit" in messages[-1]


class _Clock:
    def __init__(self) -> None:
        self._current = datetime(2026, 9, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._current
        self._current += timedelta(milliseconds=1)
        return value


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, DatasetRole]] = []

    def execute(
        self,
        target_dir: Path,
        dataset_path: Path,
        dataset_role: DatasetRole,
        *,
        max_case_concurrency: int = 1,
        max_retries: int = 3,
        retry_delay_seconds: float = 2,
        cancel_event: threading.Event | None = None,
    ) -> ExecutionResult:
        label = (target_dir / "prompt" / "system.md").read_text().strip()
        self.calls.append((label, dataset_role))
        return ExecutionResult(
            cases=(
                CaseExecution(
                    id=f"{dataset_role.value}-case",
                    result=label,
                    tracelog={"dataset": dataset_path.name},
                    metadata=ExecutionMetadata(
                        total_tokens=1,
                        duration_seconds=0.1,
                    ),
                ),
            )
        )


class _ScriptedExecutor:
    """按 Case 返回预设 retryable 序列的测试 Executor。"""

    def __init__(self, retryable_by_id: dict[str, tuple[bool, ...]]) -> None:
        self._retryable_by_id = retryable_by_id
        self._attempts_by_id = {case_id: 0 for case_id in retryable_by_id}
        self.calls: list[tuple[int, float]] = []
        self.concurrency_limits: list[int] = []
        self.cancel_events: list[threading.Event | None] = []

    def execute(
        self,
        target_dir: Path,
        dataset_path: Path,
        dataset_role: DatasetRole,
        *,
        max_case_concurrency: int = 1,
        max_retries: int = 3,
        retry_delay_seconds: float = 2,
        cancel_event: threading.Event | None = None,
    ) -> ExecutionResult:
        del target_dir, dataset_role
        self.concurrency_limits.append(max_case_concurrency)
        self.cancel_events.append(cancel_event)
        self.calls.append((max_retries, retry_delay_seconds))
        dataset_ids = tuple(
            item["id"] for item in json.loads(dataset_path.read_text(encoding="utf-8"))
        )

        def execute(case_id: str, stop: threading.Event) -> CaseExecution:
            attempt = self._attempts_by_id[case_id]
            self._attempts_by_id[case_id] += 1
            sequence = self._retryable_by_id[case_id]
            return CaseExecution(
                id=case_id,
                result=f"{case_id}-attempt-{attempt + 1}",
                tracelog={"attempt": attempt + 1},
                metadata=ExecutionMetadata(total_tokens=10, duration_seconds=0.5),
                retryable=sequence[min(attempt, len(sequence) - 1)],
            )

        return run_cases(
            dataset_ids,
            case_id=str,
            execute_once=execute,
            max_case_concurrency=max_case_concurrency,
            max_retries=max_retries,
            retry_delay_seconds=0,
            cancel_event=cancel_event,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_execute_cancellation_waits_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    """重复取消不能提前返回、持久化结果或覆盖清理故障日志。"""
    entered = threading.Event()
    stopping = threading.Event()
    release = threading.Event()
    executor = _ScriptedExecutor({"case-a": (False,)})

    def execute(
        *args: object,
        cancel_event: threading.Event,
        **kwargs: object,
    ) -> ExecutionResult:
        entered.set()
        assert cancel_event.wait(5)
        stopping.set()
        assert release.wait(5)
        if cleanup_fails:
            raise RuntimeError("cleanup failed")
        return ExecutionResult(cases=())

    errors: list[Exception] = []
    monkeypatch.setattr(executor, "execute", execute)
    monkeypatch.setattr(
        "evolution.engine.log_unexpected_exception",
        lambda logger, name, error, **kwargs: errors.append(error),
    )
    engine, repository = _retry_engine(tmp_path, executor=executor, max_retries=0)
    candidate = repository.initialize_seed()
    task = asyncio.create_task(engine._ensure_evaluation(candidate, DatasetRole.TRAIN))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(stopping.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(errors) == int(cleanup_fails)
    assert repository.load_execution(candidate.id, DatasetRole.TRAIN) is None
    assert repository.load_evaluation(candidate.id, DatasetRole.TRAIN) is None


class _Evaluator:
    def __init__(self, scores: dict[tuple[str, DatasetRole], float]) -> None:
        self._scores = scores
        self.calls: list[tuple[str, DatasetRole]] = []

    def evaluate(
        self,
        execution: ExecutionResult,
        dataset_path: Path,
    ) -> EvaluationResult:
        dataset_role = DatasetRole(dataset_path.stem)
        case = execution.cases[0]
        self.calls.append((case.result, dataset_role))
        score = self._scores[(case.result, dataset_role)]
        return EvaluationResult(
            total_score=score,
            cases=(
                CaseEvaluation(
                    id=case.id,
                    result=case.result,
                    tracelog=case.tracelog,
                    metadata=case.metadata,
                    expected={},
                    score=score,
                    feedback=f"score={score}",
                ),
            ),
        )


class _ConstantEvaluator:
    """将任意执行映射为零分，用于隔离重试编排测试。"""

    def evaluate(
        self,
        execution: ExecutionResult,
        dataset_path: Path,
    ) -> EvaluationResult:
        del dataset_path
        return EvaluationResult(
            total_score=0,
            cases=tuple(
                CaseEvaluation(
                    id=case.id,
                    result=case.result,
                    tracelog=case.tracelog,
                    metadata=case.metadata,
                    expected={},
                    score=0,
                    feedback="",
                )
                for case in execution.cases
            ),
        )


class _Improver:
    def __init__(self, changes: list[str | None]) -> None:
        self._changes: Iterator[str | None] = iter(changes)
        self.requests: list[ImprovementRequest] = []
        self.workspace_inputs: list[str] = []
        self.changelog_inputs: list[str] = []

    async def improve(self, request: ImprovementRequest) -> None:
        self.requests.append(request)
        self.changelog_inputs.append(request.changelog_path.read_text())
        self.workspace_inputs.append(
            (request.workspace_dir / "prompt" / "system.md").read_text()
        )
        change = next(self._changes)
        if change is not None:
            (request.workspace_dir / "prompt" / "system.md").write_text(
                change + "\n",
                encoding="utf-8",
            )
            _prepend_round(request, f"change-{len(self.requests)}")


def _prepend_round(request: ImprovementRequest, suffix: str = "one") -> None:
    """写入 Engine 测试所需的本轮记录，不提供虚假评测效果。"""
    previous = request.changelog_path.read_text()
    request.changelog_path.write_text(
        f"## 评测版本：{request.base_candidate_id}\n"
        f"## 迭代版本：{request.output_candidate_id}\n"
        f"### 问题类型\n{suffix}\n"
        "### 问题Case\ntrain-case\n"
        "### 文件变更\n测试代码修改\n"
        "### 变更依据\nTrain 证据\n"
        "### 相关CASE测试结果\n"
        f"CHANGE_RESULT_{request.output_candidate_id}_{suffix}\n\n" + previous
    )


def _engine(
    tmp_path: Path,
    *,
    scores: dict[tuple[str, DatasetRole], float],
    changes: list[str | None],
    max_iterations: int,
    rollback_enabled: bool = True,
    exploration_patience: int = 3,
    rollback_changelog_source: RollbackChangelogSource = "best",
) -> tuple[EvolutionEngine, CandidateRepository, _Executor, _Improver]:
    target = tmp_path / "target"
    (target / "prompt").mkdir(parents=True)
    (target / "prompt" / "system.md").write_text("seed\n", encoding="utf-8")
    task_dir = tmp_path / "task"
    datasets = task_dir / "datasets"
    datasets.mkdir(parents=True)
    dataset_paths = {role: datasets / f"{role.value}.json" for role in DatasetRole}
    for role, path in dataset_paths.items():
        path.write_text(
            json.dumps([{"id": f"{role.value}-case"}]) + "\n",
            encoding="utf-8",
        )
    executor = _Executor()
    evaluator = _Evaluator(scores)
    improver = _Improver(changes)
    repository = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="improve",
        read_only=(),
        clock=_Clock(),
    )
    task = TaskBundle(
        task_dir=task_dir,
        executor=executor,
        evaluator=evaluator,
        datasets=dataset_paths,
        digest="a" * 64,
    )
    engine = EvolutionEngine(
        repository=repository,
        task=task,
        improver=improver,
        objective="improve",
        read_only=(),
        max_iterations=max_iterations,
        rollback_enabled=rollback_enabled,
        exploration_patience=exploration_patience,
        rollback_changelog_source=rollback_changelog_source,
        max_retries=3,
        retry_delay_seconds=0,
    )
    return engine, repository, executor, improver


def _retry_engine(
    tmp_path: Path,
    *,
    executor: _ScriptedExecutor,
    max_retries: int,
    retry_delay_seconds: float = 0,
    max_case_concurrency: int = 1,
) -> tuple[EvolutionEngine, CandidateRepository]:
    """构造只用于验证 Engine 执行重试的最小环境。"""
    target = tmp_path / "target"
    target.mkdir()
    task_dir = tmp_path / "task"
    datasets = task_dir / "datasets"
    datasets.mkdir(parents=True)
    dataset_paths = {role: datasets / f"{role.value}.json" for role in DatasetRole}
    dataset_paths[DatasetRole.TRAIN].write_text(
        json.dumps([{"id": "case-a"}, {"id": "case-b"}]),
        encoding="utf-8",
    )
    for role in (DatasetRole.VALIDATION, DatasetRole.TEST):
        dataset_paths[role].write_text("[]\n", encoding="utf-8")
    repository = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="improve",
        read_only=(),
        clock=_Clock(),
    )
    task = TaskBundle(
        task_dir=task_dir,
        executor=executor,
        evaluator=_ConstantEvaluator(),
        datasets=dataset_paths,
        digest="a" * 64,
    )
    engine = EvolutionEngine(
        repository=repository,
        task=task,
        improver=_Improver([]),
        objective="improve",
        read_only=(),
        max_iterations=0,
        max_retries=max_retries,
        max_case_concurrency=max_case_concurrency,
        retry_delay_seconds=retry_delay_seconds,
    )
    return engine, repository


def _base_scores() -> dict[tuple[str, DatasetRole], float]:
    return {
        ("seed", DatasetRole.TRAIN): 0.5,
        ("seed", DatasetRole.VALIDATION): 0.7,
    }


@pytest.mark.parametrize("mode", ["latest", "best"])
async def test_validation_stagnation_rolls_back_despite_train_growth(
    tmp_path: Path,
    mode: RollbackChangelogSource,
) -> None:
    scores = {
        ("seed", DatasetRole.TRAIN): 0.6,
        ("seed", DatasetRole.VALIDATION): 0.8,
        ("b", DatasetRole.TRAIN): 0.7,
        ("b", DatasetRole.VALIDATION): 0.6,
        ("c", DatasetRole.TRAIN): 0.9,
        ("c", DatasetRole.VALIDATION): 0.7,
        ("d", DatasetRole.TRAIN): 0.5,
    }
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores=scores,
        changes=["b", "c", "d"],
        max_iterations=3,
        exploration_patience=2,
        rollback_changelog_source=mode,
    )
    result = await engine.run()
    seed, b, c, restored, d = repository.list_candidates()
    assert restored.parent_id == c.id and d.parent_id == restored.id
    assert improver.workspace_inputs == ["seed\n", "b\n", "seed\n"]
    request = improver.requests[2]
    assert request.evidence_candidate_id == restored.id
    assert request.evidence_reused_from_candidate_id == seed.id
    assert request.rollback_changelog_source == mode
    assert executor.calls.count(("seed", DatasetRole.TRAIN)) == 1
    assert executor.calls.count(("seed", DatasetRole.VALIDATION)) == 1
    evaluator = engine._task.evaluator
    assert isinstance(evaluator, _Evaluator)
    assert evaluator.calls.count(("seed", DatasetRole.TRAIN)) == 1
    assert evaluator.calls.count(("seed", DatasetRole.VALIDATION)) == 1
    assert result.iterations_attempted == 3 and result.candidates_created == 4
    assert result.best_candidate_id == seed.id
    assert restored.changelog_path in result.changelog_paths
    assert (f"CHANGE_RESULT_{c.id}" in improver.changelog_inputs[-1]) == (
        mode == "latest"
    )
    assert not (restored.directory / "test").exists()


@pytest.mark.parametrize("patience", [1, 3])
@pytest.mark.parametrize("enabled", [False, True])
async def test_rollback_patience_and_switch(
    tmp_path: Path, patience: int, enabled: bool
) -> None:
    labels = [f"step-{i}" for i in range(patience + 1)]
    scores = _base_scores() | {(label, DatasetRole.TRAIN): 0.2 for label in labels}
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores=scores,
        changes=list(labels),
        max_iterations=len(labels),
        exploration_patience=patience,
        rollback_enabled=enabled,
    )
    result = await engine.run()
    restorations = [
        c for c in repository.list_candidates() if repository.load_rollback(c.id)
    ]
    assert len(restorations) == int(enabled)
    assert improver.workspace_inputs[-1] == ("seed\n" if enabled else labels[-2] + "\n")
    assert result.candidates_created == len(labels) + int(enabled)
    assert executor.calls.count(("seed", DatasetRole.TRAIN)) == 1


async def test_default_rollback_after_three_failures_uses_best_logs(
    tmp_path: Path,
) -> None:
    scores = _base_scores() | {
        (label, DatasetRole.TRAIN): 0.2 for label in ("b", "c", "d", "e")
    }
    engine, repository, _, improver = _engine(
        tmp_path, scores=scores, changes=["b", "c", "d", "e"], max_iterations=4
    )
    await engine.run()
    seed, b, c, d, restored, e = repository.list_candidates()
    assert improver.workspace_inputs == ["seed\n", "b\n", "c\n", "seed\n"]
    assert restored.parent_id == d.id and e.parent_id == restored.id
    record = repository.load_rollback(restored.id)
    assert record is not None and record.exploration_patience == 3
    assert (
        record.changelog_source == "best"
        and record.changelog_source_candidate_id == seed.id
    )
    assert "CHANGE_RESULT" not in restored.changelog_path.read_text()


@pytest.mark.parametrize("winning", [False, True])
async def test_only_strict_validation_improvement_resets_exploration(
    tmp_path: Path, winning: bool
) -> None:
    scores = _base_scores() | {
        ("b", DatasetRole.TRAIN): 0.8,
        ("b", DatasetRole.VALIDATION): 0.9 if winning else 0.7,
        ("c", DatasetRole.TRAIN): 0.9,
        ("c", DatasetRole.VALIDATION): 0.7,
        ("d", DatasetRole.TRAIN): 0.2,
    }
    engine, repository, _, improver = _engine(
        tmp_path,
        scores=scores,
        changes=["b", "c", "d"],
        max_iterations=3,
        exploration_patience=2,
    )
    result = await engine.run()
    assert improver.workspace_inputs[-1] == ("c\n" if winning else "seed\n")
    assert result.best_candidate_id == repository.list_candidates()[int(winning)].id
    assert sum(
        repository.load_rollback(c.id) is not None for c in repository.list_candidates()
    ) == int(not winning)


def _reopen_for_rollback(
    engine: EvolutionEngine,
    repository: CandidateRepository,
    improver: _Improver,
    *,
    budget: int = 1,
    enabled: bool = True,
) -> EvolutionEngine:
    return EvolutionEngine(
        repository=repository,
        task=engine._task,
        improver=improver,
        objective="improve",
        read_only=(),
        max_iterations=budget,
        max_retries=3,
        retry_delay_seconds=0,
        rollback_enabled=enabled,
    )


async def test_rollback_no_change_and_zero_budget_do_not_publish_extra_candidates(
    tmp_path: Path,
) -> None:
    scores = _base_scores() | {
        (label, DatasetRole.TRAIN): 0.2 for label in ("b", "c", "d")
    }
    engine, repository, executor, _ = _engine(
        tmp_path, scores=scores, changes=["b", None, "c", None, "d"], max_iterations=5
    )
    result = await engine.run()
    assert result.iterations_attempted == 5 and result.candidates_created == 3
    before = list(executor.calls)
    reopened = _reopen_for_rollback(engine, repository, _Improver([]), budget=0)
    assert (await reopened.run()).candidates_created == 0
    assert len(repository.list_candidates()) == 4 and executor.calls == before
    improver = _Improver([None])
    reopened = _reopen_for_rollback(engine, repository, improver)
    assert (await reopened.run()).candidates_created == 1
    assert improver.workspace_inputs == ["seed\n"] and executor.calls == before
    resumed = _reopen_for_rollback(engine, repository, _Improver([None]), enabled=False)
    assert (await resumed.run()).candidates_created == 0
    assert executor.calls == before


@pytest.mark.parametrize("enabled", [False, True])
async def test_incomplete_restoration_fails_without_evaluation(
    tmp_path: Path, enabled: bool
) -> None:
    engine, repository, executor, _ = _engine(
        tmp_path,
        scores=_base_scores()
        | {(label, DatasetRole.TRAIN): 0.2 for label in ("b", "c", "d")},
        changes=["b", "c", "d", None],
        max_iterations=4,
    )
    await engine.run()
    restored = repository.list_candidates()[-1]
    assert repository.load_rollback(restored.id) is not None
    (restored.directory / "validation/evaluation.json").unlink()
    before = list(executor.calls)
    reopened = _reopen_for_rollback(engine, repository, _Improver([]), enabled=enabled)
    with pytest.raises(FileNotFoundError):
        await reopened.run()
    assert executor.calls == before


@pytest.mark.parametrize("enabled", [False, True])
async def test_current_best_outside_latest_lineage_controls_restoration(
    tmp_path: Path, enabled: bool
) -> None:
    scores = _base_scores() | {
        ("b", DatasetRole.TRAIN): 0.6,
        ("b", DatasetRole.VALIDATION): 0.6,
        ("branch", DatasetRole.TRAIN): 0.2,
    }
    engine, repository, executor, _ = _engine(
        tmp_path, scores=scores, changes=["b"], max_iterations=1
    )
    await engine.run()
    seed, b = repository.list_candidates()
    evaluation = repository.load_evaluation(b.id, DatasetRole.VALIDATION)
    assert evaluation is not None
    repository.save_evaluation(
        b.id,
        DatasetRole.VALIDATION,
        evaluation.model_copy(update={"total_score": 0.95}),
    )
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("branch\n")
    branch = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert branch is not None
    improver = _Improver([None])
    outcome = await _reopen_for_rollback(
        engine, repository, improver, enabled=enabled
    ).run()
    assert outcome.best_candidate_id == b.id
    assert improver.workspace_inputs == ["b\n" if enabled else "branch\n"]
    assert executor.calls.count(("b", DatasetRole.TRAIN)) == 1
    latest = repository.list_candidates()[-1]
    record = repository.load_rollback(latest.id)
    if enabled:
        assert record is not None and record.reason == "anchor_not_in_lineage"
        assert record.exploration_depth is None
        assert record.source_candidate_id == b.id and latest.parent_id == branch.id
    else:
        assert record is None and latest == branch


async def test_interruption_after_rollback_resumes_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scores = _base_scores() | {
        (label, DatasetRole.TRAIN): 0.2 for label in ("b", "c", "d")
    }
    engine, repository, executor, improver = _engine(
        tmp_path, scores=scores, changes=["b", "c", "d"], max_iterations=3
    )
    await engine.run()

    async def interrupt(request: ImprovementRequest) -> None:
        assert request.evidence_reused_from_candidate_id is not None
        raise asyncio.CancelledError

    monkeypatch.setattr(improver, "improve", interrupt)
    before = list(executor.calls)
    with pytest.raises(asyncio.CancelledError):
        await _reopen_for_rollback(engine, repository, improver).run()
    restored = repository.list_candidates()[-1]
    assert repository.load_rollback(restored.id) is not None
    resumed = _Improver([None])
    outcome = await _reopen_for_rollback(engine, repository, resumed).run()
    assert outcome.candidates_created == 0
    assert executor.calls == before
    assert resumed.requests[0].evidence_candidate_id == restored.id


async def test_manual_test_is_independent_of_training_and_parent_results(
    tmp_path: Path,
) -> None:
    """Test 只运行指定 Target，不要求父级或当前候选有训练结果。"""
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores={("child", DatasetRole.TEST): 0.8},
        changes=[],
        max_iterations=0,
    )
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("child\n")
    child = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert child is not None
    before = {
        p: p.read_bytes()
        for candidate in repository.list_candidates()
        for p in candidate.directory.rglob("*")
        if p.is_file()
    }

    result = await engine.run_test(child.id)

    assert result.total_score == 0.8
    assert executor.calls == [("child", DatasetRole.TEST)]
    assert not improver.requests
    assert len(repository.list_candidates()) == 2
    assert repository.load_evaluation(child.id, DatasetRole.TEST) == result
    assert all(p.read_bytes() == value for p, value in before.items())


async def test_manual_test_failure_preserves_previous_results(tmp_path: Path) -> None:
    """重复 Test 的评分失败时，旧 Test 执行和评分仍保持配对。"""
    scores = {("seed", DatasetRole.TEST): 0.8}
    engine, repository, executor, _ = _engine(
        tmp_path,
        scores=scores,
        changes=[],
        max_iterations=0,
    )
    seed = repository.initialize_seed()
    await engine.run_test(seed.id)
    old = {p: p.read_bytes() for p in (seed.directory / "test").glob("*.json")}
    scores.clear()
    with pytest.raises(KeyError):
        await engine.run_test(seed.id)
    assert all(p.read_bytes() == value for p, value in old.items())
    scores[("seed", DatasetRole.TEST)] = 0.9
    result = await engine.run_test(seed.id)
    assert result.total_score == 0.9
    assert len(executor.calls) == 3
    assert repository.load_evaluation(seed.id, DatasetRole.TEST) == result


async def test_manual_test_reevaluation_reuses_saved_execution(tmp_path: Path) -> None:
    """Test 重评只更新指定候选的评分，不执行或修改执行文件。"""
    scores = {("seed", DatasetRole.TEST): 0.8}
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores=scores,
        changes=[],
        max_iterations=0,
    )
    seed = repository.initialize_seed()
    with pytest.raises(ValueError, match="Execution"):
        await engine.reevaluate_test(seed.id)
    assert not executor.calls
    await engine.run_test(seed.id)
    path = seed.directory / "test/execution.json"
    execution = path.read_bytes()
    modified_at = path.stat().st_mtime_ns
    evaluation_path = seed.directory / "test/evaluation.json"
    old_evaluation = evaluation_path.read_bytes()
    scores.clear()
    with pytest.raises(KeyError):
        await engine.reevaluate_test(seed.id)
    assert evaluation_path.read_bytes() == old_evaluation
    evaluation_path.unlink()
    scores[("seed", DatasetRole.TEST)] = 0.9

    updated = await engine.reevaluate_test(seed.id)

    assert updated.total_score == 0.9
    assert path.read_bytes() == execution
    assert path.stat().st_mtime_ns == modified_at
    assert executor.calls == [("seed", DatasetRole.TEST)]
    assert not improver.requests
    assert repository.load_evaluation(seed.id, DatasetRole.TEST) == updated


class _TwoCaseExecutor:
    """按数据集 ID 逐个产出 Case 的测试 Executor。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, DatasetRole]] = []

    def execute(
        self,
        target_dir: Path,
        dataset_path: Path,
        dataset_role: DatasetRole,
        *,
        max_case_concurrency: int = 1,
        max_retries: int = 3,
        retry_delay_seconds: float = 2,
        cancel_event: threading.Event | None = None,
    ) -> ExecutionResult:
        label = (target_dir / "prompt" / "system.md").read_text().strip()
        self.calls.append((label, dataset_role))
        metadata = ExecutionMetadata(total_tokens=1, duration_seconds=0.1)
        return ExecutionResult(
            cases=tuple(
                CaseExecution(
                    id=item["id"],
                    result=label,
                    tracelog={},
                    metadata=metadata,
                )
                for item in json.loads(dataset_path.read_text(encoding="utf-8"))
            )
        )


class _FeedbackEvaluator:
    """按（版本, Case）返回预设分数与反馈文本的测试 Evaluator。"""

    def __init__(self, cases: dict[tuple[str, str], tuple[float, str]]) -> None:
        self._cases = cases

    def evaluate(
        self,
        execution: ExecutionResult,
        dataset_path: Path,
    ) -> EvaluationResult:
        del dataset_path
        scored = tuple(
            CaseEvaluation(
                id=case.id,
                result=case.result,
                tracelog=case.tracelog,
                metadata=case.metadata,
                expected={},
                score=self._cases[(case.result, case.id)][0],
                feedback=self._cases[(case.result, case.id)][1],
            )
            for case in execution.cases
        )
        total = sum(case.score for case in scored) / len(scored) if scored else 0.0
        return EvaluationResult(total_score=total, cases=scored)


async def test_single_case_clusters_defer_until_problem_library_matures(
    tmp_path: Path,
) -> None:
    """单 case 簇本轮延后入库；同签名新 case 积累到阈值后转为可处理。"""
    target = tmp_path / "target"
    (target / "prompt").mkdir(parents=True)
    (target / "prompt" / "system.md").write_text("seed\n", encoding="utf-8")
    task_dir = tmp_path / "task"
    datasets = task_dir / "datasets"
    datasets.mkdir(parents=True)
    dataset_paths = {role: datasets / f"{role.value}.json" for role in DatasetRole}
    dataset_paths[DatasetRole.TRAIN].write_text(
        json.dumps([{"id": "case-a"}, {"id": "case-b"}]), encoding="utf-8"
    )
    dataset_paths[DatasetRole.VALIDATION].write_text(
        json.dumps([{"id": "validation-case"}]), encoding="utf-8"
    )
    dataset_paths[DatasetRole.TEST].write_text("[]", encoding="utf-8")
    executor = _TwoCaseExecutor()
    evaluator = _FeedbackEvaluator(
        {
            ("seed", "case-a"): (0.0, "Error: tool call failed"),
            ("seed", "case-b"): (0.0, "wrong answer format"),
            ("seed", "validation-case"): (0.5, ""),
            ("v1", "case-a"): (1.0, ""),
            ("v1", "case-b"): (0.0, "Error: tool call failed"),
            ("v1", "validation-case"): (0.6, ""),
        }
    )
    improver = _Improver(["v1", None])
    repository = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="improve",
        read_only=(),
        clock=_Clock(),
    )
    engine = EvolutionEngine(
        repository=repository,
        task=TaskBundle(
            task_dir=task_dir,
            executor=executor,
            evaluator=evaluator,
            datasets=dataset_paths,
            digest="a" * 64,
        ),
        improver=improver,
        objective="improve",
        read_only=(),
        max_iterations=2,
        max_retries=3,
        retry_delay_seconds=0,
    )

    result = await engine.run()

    assert result.candidates_created == 1
    first, second = improver.requests
    assert first.diagnosis_brief.clusters == ()
    assert len(first.deferred_clusters) == 2
    assert first.problem_library is not None
    assert all(
        entry.status == "accumulating" for entry in first.problem_library.entries
    )
    assert len(second.diagnosis_brief.clusters) == 1
    cluster = second.diagnosis_brief.clusters[0]
    assert tuple(case.id for case in cluster.cases) == ("case-b",)
    assert second.deferred_clusters == ()
    library = repository.load_problem_library()
    by_signature = {entry.signature: entry for entry in library.entries}
    tool_call = by_signature["error_tool_call_failed"]
    assert tool_call.case_ids == ("case-a", "case-b")
    assert tool_call.status == "ready"
    wrong_format = by_signature["wrong_answer_format"]
    assert wrong_format.case_ids == ("case-b",)
    assert wrong_format.status == "resolved"


async def test_execute_retries_only_retryable_cases_and_preserves_attempts(
    tmp_path: Path,
) -> None:
    """成功 Case 不重跑，失败 Case 保留每次事实并累计资源。"""
    executor = _ScriptedExecutor({"case-a": (True, False), "case-b": (False,)})
    engine, repository = _retry_engine(
        tmp_path,
        executor=executor,
        max_retries=3,
        max_case_concurrency=2,
    )
    candidate = repository.initialize_seed()

    result = await engine._execute(candidate, DatasetRole.TRAIN)  # noqa: SLF001

    assert executor.calls == [(3, 0)]
    assert executor.concurrency_limits == [2]
    assert len(executor.cancel_events) == 1
    assert all(isinstance(event, threading.Event) for event in executor.cancel_events)
    assert tuple(case.id for case in result.cases) == ("case-a", "case-b")
    retried, untouched = result.cases
    assert retried.result == "case-a-attempt-2"
    assert len(retried.attempts) == 2
    assert retried.metadata == ExecutionMetadata(
        total_tokens=20,
        duration_seconds=1,
    )
    assert untouched.result == "case-b-attempt-1"
    assert untouched.attempts == ()


async def test_engine_forwards_retry_policy_without_own_retry_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Engine 只调用一次 Executor，转交策略而不自行等待或二次重试。"""
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("evolution.engine.asyncio.sleep", record_sleep)
    executor = _ScriptedExecutor({"case-a": (True,), "case-b": (False,)})
    engine, repository = _retry_engine(
        tmp_path,
        executor=executor,
        max_retries=3,
        retry_delay_seconds=2,
    )
    candidate = repository.initialize_seed()

    result = await engine._execute(candidate, DatasetRole.TRAIN)  # noqa: SLF001

    assert executor.calls == [(3, 2)]
    assert delays == []
    retried = result.cases[0]
    assert retried.result == "case-a-attempt-4"
    assert len(retried.attempts) == 4
    assert retried.metadata.total_tokens == 40


async def test_zero_max_retries_executes_each_case_only_once(tmp_path: Path) -> None:
    """max_retries=0 必须禁用所有额外尝试。"""
    executor = _ScriptedExecutor({"case-a": (True,), "case-b": (False,)})
    engine, repository = _retry_engine(
        tmp_path,
        executor=executor,
        max_retries=0,
    )
    candidate = repository.initialize_seed()

    result = await engine._execute(candidate, DatasetRole.TRAIN)  # noqa: SLF001

    assert executor.calls == [(0, 0)]
    assert result.cases[0].retryable is True
    assert result.cases[0].attempts == ()


async def test_missing_evaluation_delegates_retry_policy(tmp_path: Path) -> None:
    """缺失阶段只调用一次 Executor，由后者聚合尝试。"""
    executor = _ScriptedExecutor({"case-a": (True, False), "case-b": (False,)})
    engine, repository = _retry_engine(
        tmp_path,
        executor=executor,
        max_retries=3,
    )
    candidate = repository.initialize_seed()

    evaluation = await engine._ensure_evaluation(candidate, DatasetRole.TRAIN)

    assert executor.calls == [(3, 0)]
    assert evaluation.cases[0].metadata == ExecutionMetadata(
        total_tokens=20,
        duration_seconds=1,
    )


async def test_failed_train_becomes_next_evidence_and_workspace(tmp_path: Path) -> None:
    """Train 下降后仍继承最新候选的代码、日志和证据。"""
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores=_base_scores()
        | {
            ("bad", DatasetRole.TRAIN): 0.2,
            ("next", DatasetRole.TRAIN): 0.3,
        },
        changes=["bad", "next"],
        max_iterations=2,
    )
    await engine.run()
    seed, bad, next_candidate = repository.list_candidates()
    request = improver.requests[1]
    assert request.base_candidate_id == bad.id
    assert request.train_evaluation.total_score == 0.2
    assert request.evidence_candidate_id == bad.id
    assert request.evidence_parent_candidate_id == seed.id
    assert request.changelog_source_candidate_id == bad.id
    assert improver.changelog_inputs[-1] == bad.changelog_path.read_text()
    assert next_candidate.parent_id == bad.id
    assert improver.workspace_inputs == ["seed\n", "bad\n"]
    assert improver.requests[0].evidence_candidate_id == seed.id
    assert improver.requests[0].evidence_parent_candidate_id is None
    evaluation = repository.load_evaluation(next_candidate.id, DatasetRole.TRAIN)
    assert evaluation is not None and evaluation.parent_comparison is not None
    assert evaluation.parent_comparison.previous_candidate_id == bad.id
    assert evaluation.parent_comparison.total_score_delta == pytest.approx(0.1)
    assert executor.calls.count(("seed", DatasetRole.TRAIN)) == 1


@pytest.mark.parametrize("validation_score", [0.6, 0.9])
async def test_latest_evidence_survives_validation_and_no_change(
    tmp_path: Path, validation_score: float
) -> None:
    """验证结果不影响最新 Workspace；NO_CHANGE 不使来源回退。"""
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores=_base_scores()
        | {
            ("better", DatasetRole.TRAIN): 0.8,
            ("better", DatasetRole.VALIDATION): validation_score,
        },
        changes=["better", None, None],
        max_iterations=3,
    )
    await engine.run()
    seed, better = repository.list_candidates()
    for request in improver.requests[1:]:
        assert request.base_candidate_id == better.id
        assert request.evidence_candidate_id == better.id
        assert request.evidence_parent_candidate_id == seed.id
        assert request.train_evaluation.total_score == 0.8

    assert executor.calls.count(("better", DatasetRole.TRAIN)) == 1
    expected_input = "better\n"
    assert improver.workspace_inputs[1:] == [expected_input, expected_input]
    assert improver.changelog_inputs[1:] == [better.changelog_path.read_text()] * 2


async def test_reopened_engine_uses_latest_complete_candidate_train(
    tmp_path: Path,
) -> None:
    """重启先补齐最新版本评分，再按候选顺序选择证据。"""
    scores = _base_scores() | {
        ("bad", DatasetRole.TRAIN): 0.2,
        ("unfinished", DatasetRole.TRAIN): 0.5,
        ("unfinished", DatasetRole.VALIDATION): 0.6,
    }
    engine, repository, executor, _ = _engine(
        tmp_path, scores=scores, changes=["bad"], max_iterations=1
    )
    await engine.run()
    seed, bad = repository.list_candidates()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target" / "prompt" / "system.md").write_text("unfinished\n")
    unfinished = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert unfinished is not None
    repository.save_execution(
        unfinished.id,
        DatasetRole.TRAIN,
        executor.execute(
            unfinished.target_dir,
            tmp_path / "task/datasets/train.json",
            DatasetRole.TRAIN,
        ),
    )
    improver = _Improver([None])
    reopened = EvolutionEngine(
        repository=repository,
        task=TaskBundle(
            task_dir=tmp_path / "task",
            executor=executor,
            evaluator=_Evaluator(scores),
            datasets={
                role: tmp_path / "task/datasets" / f"{role.value}.json"
                for role in DatasetRole
            },
            digest="a" * 64,
        ),
        improver=improver,
        objective="improve",
        read_only=(),
        max_iterations=1,
        max_retries=3,
        retry_delay_seconds=0,
    )
    calls_before = list(executor.calls)
    await reopened.run()
    request = improver.requests[0]
    assert request.base_candidate_id == unfinished.id
    assert request.evidence_candidate_id == unfinished.id
    assert request.train_evaluation.total_score == 0.5
    assert improver.changelog_inputs[-1] == unfinished.changelog_path.read_text()
    assert executor.calls == calls_before + [("unfinished", DatasetRole.VALIDATION)]


async def test_zero_candidates_only_initializes_seed(tmp_path: Path) -> None:
    """max_iterations=0 必须只执行 Seed Train 和 Validation。"""
    engine, _, executor, _ = _engine(
        tmp_path,
        scores=_base_scores(),
        changes=[],
        max_iterations=0,
    )

    result = await engine.run()

    assert result.seed_candidate_id == result.best_candidate_id
    assert result.iterations_attempted == 0
    assert result.candidates_created == 0
    assert result.stop_reason is StopReason.ITERATION_LIMIT
    assert executor.calls == [
        ("seed", DatasetRole.TRAIN),
        ("seed", DatasetRole.VALIDATION),
    ]


async def test_train_tie_executes_validation(tmp_path: Path) -> None:
    """Train 与最佳持平的 Candidate 执行 Validation。"""
    scores = _base_scores() | {
        ("tie", DatasetRole.TRAIN): 0.5,
        ("tie", DatasetRole.VALIDATION): 0.6,
    }
    engine, repository, executor, _ = _engine(
        tmp_path,
        scores=scores,
        changes=["tie"],
        max_iterations=1,
    )

    result = await engine.run()

    assert result.validation_score == 0.7
    assert executor.calls[-1] == ("tie", DatasetRole.VALIDATION)
    challenger = repository.list_candidates()[-1]
    challenger_evaluation = repository.load_evaluation(
        challenger.id,
        DatasetRole.TRAIN,
    )
    assert challenger_evaluation is not None
    comparison = challenger_evaluation.parent_comparison
    assert comparison is not None and comparison.total_score_delta == 0
    assert len(result.changelog_paths) == 1
    assert result.changelog_paths[0].parent == challenger.directory
    assert "change-1" in result.changelog_paths[0].read_text(encoding="utf-8")


async def test_train_winner_with_lower_validation_does_not_replace_best(
    tmp_path: Path,
) -> None:
    """Train 只授予 Validation 资格，最终仍按 Validation 守擂。"""
    scores = _base_scores() | {
        ("challenger", DatasetRole.TRAIN): 0.8,
        ("challenger", DatasetRole.VALIDATION): 0.6,
    }
    engine, repository, executor, _ = _engine(
        tmp_path,
        scores=scores,
        changes=["challenger"],
        max_iterations=1,
    )

    result = await engine.run()

    assert result.best_candidate_id == repository.list_candidates()[0].id
    assert ("challenger", DatasetRole.VALIDATION) in executor.calls


@pytest.mark.parametrize(
    ("train_score", "validation_score", "conclusion"),
    [
        (0.4, None, "未执行 Validation，保留原最佳版本。"),
        (0.5, 0.7, "Validation：0.7000 → 0.7000（+0.0000），保留原最佳版本。"),
        (0.8, 0.6, "Validation：0.7000 → 0.6000（−0.1000），保留原最佳版本。"),
        (0.8, 0.7, "Validation：0.7000 → 0.7000（+0.0000），保留原最佳版本。"),
        (0.8, 0.9, "Validation：0.7000 → 0.9000（+0.2000），成为新的最佳版本。"),
    ],
)
async def test_platform_backfills_round_results(
    tmp_path: Path, train_score: float, validation_score: float | None, conclusion: str
) -> None:
    """最后一轮也回填客观结果，恢复时不覆盖历史结论或行为分析槽位。"""
    scores = _base_scores() | {("challenger", DatasetRole.TRAIN): train_score}
    if validation_score is not None:
        scores[("challenger", DatasetRole.VALIDATION)] = validation_score
    engine, repository, _, _ = _engine(
        tmp_path, scores=scores, changes=["challenger"], max_iterations=1
    )
    await engine.run()
    candidate = repository.list_candidates()[-1]
    log = candidate.changelog_path.read_text()
    assert conclusion in log
    seed = repository.list_candidates()[0]
    relation = ">=" if train_score >= 0.5 else "<"
    assert (
        f"验证门槛：{candidate.id} Train {train_score:.4f} {relation} "
        f"最佳版本 {seed.id} Train 0.5000。" in log
    )
    assert "若有下一轮优化预算，继续从本候选探索。" in log
    assert f"训练集：0.5000 → {train_score:.4f}" in log
    assert "改善 " in log and "退步 " in log and "不变 " in log
    assert "CHANGE_RESULT_" in log
    assert "EVALUATION_COMPARISON_" not in log
    assert "ADOPTION_RESULT_" not in log
    assert "验证集" not in log.split("### 评测对比")[1].split("### 结论")[0]
    await engine._ensure_candidate_evaluated(candidate)
    assert candidate.changelog_path.read_text() == log
    assert candidate.changelog_path.stat().st_mode & 0o200


async def test_improvement_request_carries_diagnosis_brief(tmp_path: Path) -> None:
    """改进请求携带由证据评测确定性生成的诊断简报。

    唯一失败簇是 partial 单 case 簇，未成熟前延后进问题库，
    可处理简报不再包含该簇。
    """
    engine, repository, _, improver = _engine(
        tmp_path, scores=_base_scores(), changes=[None], max_iterations=1
    )
    await engine.run()
    request = improver.requests[0]
    brief = request.diagnosis_brief
    assert brief.evidence_candidate_id == request.evidence_candidate_id
    assert brief.total_score == 0.5
    assert brief.clusters == ()
    assert len(request.deferred_clusters) == 1
    cluster = request.deferred_clusters[0]
    assert tuple(case.id for case in cluster.cases) == ("train-case",)
    assert brief.passing_case_ids == ()
    assert brief.regressed_cases == ()
    assert request.problem_library is not None
    assert len(request.problem_library.entries) == 1
    assert request.problem_library.entries[0].case_ids == ("train-case",)
    assert repository.load_problem_library() == request.problem_library


async def test_backfill_lists_regressed_case_ids(tmp_path: Path) -> None:
    """宽松门放行的总分比较之外，退步 Case 在评测对比中显式留痕。"""
    engine, repository, _, _ = _engine(
        tmp_path,
        scores=_base_scores() | {("challenger", DatasetRole.TRAIN): 0.4},
        changes=["challenger"],
        max_iterations=1,
    )
    await engine.run()
    log = repository.list_candidates()[-1].changelog_path.read_text()
    assert "退步 case：train-case。" in log

    engine, repository, _, _ = _engine(
        tmp_path / "second",
        scores=_base_scores()
        | {
            ("challenger", DatasetRole.TRAIN): 0.8,
            ("challenger", DatasetRole.VALIDATION): 0.9,
        },
        changes=["challenger"],
        max_iterations=1,
    )
    await engine.run()
    log = repository.list_candidates()[-1].changelog_path.read_text()
    assert "退步 case：无。" in log


async def test_backfill_recovers_after_evaluation_without_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评测落盘后回填中断，恢复只补日志；下一轮继承已回填结果。"""
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores=_base_scores()
        | {
            ("better", DatasetRole.TRAIN): 0.8,
            ("better", DatasetRole.VALIDATION): 0.9,
        },
        changes=["better", None],
        max_iterations=1,
    )
    with monkeypatch.context() as patch:

        def interrupt(*args: object, **kwargs: object) -> None:
            raise OSError("interrupted backfill")

        patch.setattr(repository, "backfill_changelog", interrupt)
        with pytest.raises(OSError, match="interrupted backfill"):
            await engine.run()
    candidate = repository.list_candidates()[-1]
    assert "### 结论" not in candidate.changelog_path.read_text()
    executor.calls.clear()
    await engine.run()
    assert executor.calls == []
    log = candidate.changelog_path.read_text()
    assert "成为新的最佳版本" in log
    assert improver.changelog_inputs[-1] == log


async def test_run_refreshes_backfill_after_train_directory_removed(
    tmp_path: Path,
) -> None:
    """删除 Train 后主流程重评并覆盖旧总结，其他历史与分析原样保留。"""
    scores = _base_scores() | {("challenger", DatasetRole.TRAIN): 0.4}
    engine, repository, executor, improver = _engine(
        tmp_path, scores=scores, changes=["challenger", None], max_iterations=1
    )
    await engine.run()
    seed, candidate = repository.list_candidates()
    old_log = candidate.changelog_path.read_text()
    assert "训练集：0.5000 → 0.4000" in old_log
    prefix = old_log.split("### 评测对比\n")[0]
    seed_log = seed.changelog_path.read_bytes()
    train_dir = candidate.directory / "train"
    (train_dir / "execution.json").unlink()
    (train_dir / "evaluation.json").unlink()
    train_dir.rmdir()
    scores[("challenger", DatasetRole.TRAIN)] = 0.8
    scores[("challenger", DatasetRole.VALIDATION)] = 0.9
    executor.calls.clear()

    await engine.run()

    log = candidate.changelog_path.read_text()
    assert log.startswith(prefix)
    assert "训练集：0.5000 → 0.8000" in log
    assert "成为新的最佳版本" in log
    assert "Train 0.8000 >=" in log
    assert "训练集：0.5000 → 0.4000" not in log
    assert "未执行 Validation" not in log
    assert seed.changelog_path.read_bytes() == seed_log
    assert improver.changelog_inputs[-1] == log
    assert executor.calls == [
        ("challenger", DatasetRole.TRAIN),
        ("challenger", DatasetRole.VALIDATION),
    ]


async def test_no_change_continues_until_iteration_limit(tmp_path: Path) -> None:
    """Improver 无净变化时仍须执行满改进轮数且不发布空 Candidate。"""
    engine, repository, _, improver = _engine(
        tmp_path,
        scores=_base_scores(),
        changes=[None] * 5,
        max_iterations=5,
    )

    result = await engine.run()

    assert result.stop_reason is StopReason.ITERATION_LIMIT
    assert result.iterations_attempted == 5
    assert result.candidates_created == 0
    assert len(repository.list_candidates()) == 1
    assert len(improver.requests) == 5
    assert all(
        request.train_evaluation.total_score == 0.5 for request in improver.requests
    )
    assert improver.changelog_inputs == [""] * 5
    seed = repository.list_candidates()[0]
    assert seed.changelog_path.read_text() == ""
    assert result.changelog_paths == ()
    assert all(
        not request.workspace_dir.parent.exists() for request in improver.requests
    )


async def test_core_run_never_executes_test_role(tmp_path: Path) -> None:
    """无论 Candidate 是否晋级，核心 run 都不得读取 Test。"""
    scores = _base_scores() | {
        ("better", DatasetRole.TRAIN): 0.8,
        ("better", DatasetRole.VALIDATION): 0.9,
    }
    engine, _, executor, _ = _engine(
        tmp_path,
        scores=scores,
        changes=["better"],
        max_iterations=1,
    )

    await engine.run()

    assert DatasetRole.TEST not in {role for _, role in executor.calls}


@pytest.mark.parametrize("missing_manifest", [False, True])
async def test_reopening_incomplete_experiment_recovers_seed(
    tmp_path: Path,
    missing_manifest: bool,
) -> None:
    """已有 Seed 缺失评分时补齐必要阶段。"""
    engine, repository, _, _ = _engine(
        tmp_path,
        scores=_base_scores(),
        changes=[],
        max_iterations=0,
    )
    original = repository.initialize_seed()
    if missing_manifest:
        (original.directory.parent.parent / "manifest.json").unlink()

    await engine.run()
    seed = repository.initialize_seed()
    assert seed.id == original.id
    assert repository.load_evaluation(seed.id, DatasetRole.TRAIN) is not None
    assert repository.load_evaluation(seed.id, DatasetRole.VALIDATION) is not None


@pytest.mark.parametrize("is_seed", [False, True])
@pytest.mark.parametrize("completed_stages", range(5))
async def test_run_resumes_only_missing_stages(
    tmp_path: Path, is_seed: bool, completed_stages: int
) -> None:
    """重建 Engine 后按落盘阶段继续，完整结果保持原字节。"""
    scores = _base_scores() | {
        ("child", DatasetRole.TRAIN): 0.5,
        ("child", DatasetRole.VALIDATION): 0.9,
    }
    engine, repository, executor, _ = _engine(
        tmp_path, scores=scores, changes=[], max_iterations=0
    )
    seed = repository.initialize_seed()
    candidate = seed
    if not is_seed:
        await engine._ensure_evaluation(seed, DatasetRole.TRAIN)
        await engine._ensure_evaluation(seed, DatasetRole.VALIDATION)
        workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
        (workspace / "target/prompt/system.md").write_text("child\n")
        published = repository.publish_workspace(
            seed.id, workspace, candidate_id=repository.allocate_candidate_id()
        )
        assert published is not None
        candidate = published
    evaluator = _Evaluator(scores)
    roles = (DatasetRole.TRAIN, DatasetRole.VALIDATION)
    for index, role in enumerate(roles):
        if completed_stages > index * 2:
            execution = executor.execute(candidate.target_dir, tmp_path, role)
            repository.save_execution(candidate.id, role, execution)
        if completed_stages > index * 2 + 1:
            await engine._ensure_evaluation(candidate, role)
    preserved = {
        path: path.read_bytes() for path in candidate.directory.glob("*/*.json")
    }
    executor.calls.clear()
    improver = _Improver([])
    reopened = EvolutionEngine(
        repository=repository,
        task=TaskBundle(
            task_dir=tmp_path / "task",
            executor=executor,
            evaluator=evaluator,
            datasets={role: tmp_path / f"{role}.json" for role in DatasetRole},
            digest="a" * 64,
        ),
        improver=improver,
        objective="improve",
        read_only=(),
        max_iterations=0,
        max_retries=0,
        retry_delay_seconds=0,
    )
    result = await reopened.run()
    label = "seed" if is_seed else "child"
    assert executor.calls == [
        (label, role) for i, role in enumerate(roles) if completed_stages <= 2 * i
    ]
    assert evaluator.calls == [
        (label, role) for i, role in enumerate(roles) if completed_stages <= 2 * i + 1
    ]
    assert result.iterations_attempted == result.candidates_created == 0
    assert not improver.requests
    assert all(path.read_bytes() == content for path, content in preserved.items())
    if not is_seed:
        train = repository.load_evaluation(candidate.id, DatasetRole.TRAIN)
        validation = repository.load_evaluation(candidate.id, DatasetRole.VALIDATION)
        assert train is not None and train.parent_comparison is not None
        assert train.parent_comparison.previous_candidate_id == seed.id
        assert train.cases[0].previous_score == scores[("seed", DatasetRole.TRAIN)]
        assert validation is not None
        assert validation.cases[0].previous_score == 0.7


@pytest.mark.parametrize("stage", range(4))
@pytest.mark.parametrize("after_save", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_run_resumes_after_stage_save_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: int,
    after_save: bool,
    cancel: bool,
) -> None:
    """保存前后中断时重建 Engine，只有未落盘阶段会重做。"""
    engine, repository, executor, _ = _engine(
        tmp_path, scores=_base_scores(), changes=[], max_iterations=0
    )
    role = (DatasetRole.TRAIN, DatasetRole.VALIDATION)[stage // 2]
    method = "save_evaluation" if stage % 2 else "save_execution"
    original = getattr(repository, method)

    def interrupt(candidate_id: str, saved_role: DatasetRole, value: object) -> None:
        if saved_role != role or after_save:
            original(candidate_id, saved_role, value)
        if saved_role == role:
            if cancel:
                raise asyncio.CancelledError
            raise OSError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(repository, method, interrupt)
        with pytest.raises(asyncio.CancelledError if cancel else OSError):
            await engine.run()
    saved = {
        path: path.read_bytes()
        for path in repository.initialize_seed().directory.glob("*/*.json")
    }
    evaluator = _Evaluator(_base_scores())
    executor.calls.clear()
    reopened = EvolutionEngine(
        repository=repository,
        task=TaskBundle(
            task_dir=tmp_path / "task",
            executor=executor,
            evaluator=evaluator,
            datasets={r: tmp_path / f"{r}.json" for r in DatasetRole},
            digest="a" * 64,
        ),
        improver=_Improver([]),
        objective="improve",
        read_only=(),
        max_iterations=0,
        max_retries=0,
        retry_delay_seconds=0,
    )
    await reopened.run()
    completed = stage + int(after_save)
    roles = (DatasetRole.TRAIN, DatasetRole.VALIDATION)
    assert executor.calls == [
        ("seed", r) for i, r in enumerate(roles) if completed <= 2 * i
    ]
    assert evaluator.calls == [
        ("seed", r) for i, r in enumerate(roles) if completed <= 2 * i + 1
    ]
    assert all(path.read_bytes() == value for path, value in saved.items())


async def test_recovery_uses_current_stored_best_train(
    tmp_path: Path,
) -> None:
    """恢复门槛取当前最佳 Train，已保存父子比较不变，仍继续迭代。"""
    scores = _base_scores() | {
        ("better", DatasetRole.TRAIN): 0.8,
        ("better", DatasetRole.VALIDATION): 0.9,
    }
    engine, repository, executor, improver = _engine(
        tmp_path, scores=scores, changes=["better", None], max_iterations=1
    )
    await engine.run()
    seed, child = repository.list_candidates()
    (child.directory / "validation/evaluation.json").unlink()
    train = repository.load_evaluation(child.id, DatasetRole.TRAIN)
    assert train is not None
    for role in (DatasetRole.TRAIN, DatasetRole.VALIDATION):
        evaluation = repository.load_evaluation(seed.id, role)
        assert evaluation is not None
        repository.save_evaluation(
            seed.id,
            role,
            evaluation.model_copy(
                update={
                    "total_score": 1.0,
                    "cases": tuple(
                        case.model_copy(update={"score": 1.0})
                        for case in evaluation.cases
                    ),
                }
            ),
        )
    before = list(executor.calls)

    result = await engine.run()

    assert executor.calls == before
    assert result.stop_reason is StopReason.ITERATION_LIMIT
    assert result.iterations_attempted == 1
    assert repository.load_evaluation(child.id, DatasetRole.VALIDATION) is None
    assert repository.load_evaluation(child.id, DatasetRole.TRAIN) == train
    assert improver.requests[-1].base_candidate_id == child.id
    assert improver.requests[-1].evidence_candidate_id == child.id


async def test_recovery_orders_pending_candidates_and_stops_on_failure(
    tmp_path: Path,
) -> None:
    """多个未完成版本按父级在先恢复，失败不会越过到子级或优化。"""
    scores = _base_scores()
    engine, repository, executor, improver = _engine(
        tmp_path, scores=scores, changes=[None], max_iterations=1
    )
    seed = repository.initialize_seed()
    await engine._ensure_evaluation(seed, DatasetRole.TRAIN)
    await engine._ensure_evaluation(seed, DatasetRole.VALIDATION)
    parent = seed
    for label in ("first", "second"):
        workspace = repository.create_workspace(
            parent.id, changelog_source_id=parent.id
        )
        (workspace / "target/prompt/system.md").write_text(label)
        published = repository.publish_workspace(
            parent.id, workspace, candidate_id=repository.allocate_candidate_id()
        )
        assert published is not None
        parent = published
    executor.calls.clear()
    with pytest.raises(KeyError):
        await engine.run()
    assert executor.calls == [("first", DatasetRole.TRAIN)]
    assert not improver.requests
    scores.update(
        {
            ("first", DatasetRole.TRAIN): 0.8,
            ("first", DatasetRole.VALIDATION): 0.8,
            ("second", DatasetRole.TRAIN): 0.9,
            ("second", DatasetRole.VALIDATION): 0.9,
        }
    )
    await engine.run()
    assert executor.calls == [
        ("first", DatasetRole.TRAIN),
        ("first", DatasetRole.VALIDATION),
        ("second", DatasetRole.TRAIN),
        ("second", DatasetRole.VALIDATION),
    ]
    assert len(improver.requests) == 1
    assert improver.requests[0].evidence_candidate_id == parent.id


@pytest.mark.parametrize("parent", ["candidate-20990101T000000", "self", ""])
async def test_recovery_rejects_invalid_parent_before_running_stages(
    tmp_path: Path, parent: str
) -> None:
    """先校验父级图，损坏引用不能触发执行或评分。"""
    engine, repository, executor, improver = _engine(
        tmp_path, scores=_base_scores(), changes=[], max_iterations=1
    )
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("child")
    child = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert child is not None
    path = child.directory / "parent"
    path.chmod(0o644)
    path.write_text(child.id if parent == "self" else parent)
    with pytest.raises(ValueError, match="父级"):
        await engine.run()
    assert not executor.calls
    assert not improver.requests


async def test_retry_keeps_rejected_log_source_and_discards_failed_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败重试固定继承落选日志，不能继承失败草稿或回退最优日志。"""
    engine, repository, _, improver = _engine(
        tmp_path,
        scores=_base_scores() | {("bad", DatasetRole.TRAIN): 0.2},
        changes=["bad"],
        max_iterations=1,
    )
    await engine.run()
    seed, bad = repository.list_candidates()
    inputs: list[str] = []

    async def improve(request: ImprovementRequest) -> None:
        assert request.base_candidate_id == bad.id
        assert request.changelog_source_candidate_id == bad.id
        inputs.append(request.changelog_path.read_text())
        request.changelog_path.write_text("failed draft")
        if len(inputs) == 1:
            raise ValueError("retry")

    monkeypatch.setattr(improver, "improve", improve)
    monkeypatch.setattr("evolution.engine._IMPROVER_RETRY_DELAY_SECONDS", 0)
    result = await engine.run()
    assert inputs == [bad.changelog_path.read_text()] * 2
    assert result.candidates_created == 0
    assert repository.list_candidates() == (seed, bad)


def test_compare_evaluations_counts_case_directions() -> None:
    """相同 Case 集合必须给出逐 Case 改善、退化和不变数量。"""
    previous = _result(("a", 0.2), ("b", 0.5), ("c", 0.7))
    current = _result(("a", 0.4), ("b", 0.3), ("c", 0.7))

    comparison = compare_evaluations("old", previous, "new", current)

    assert comparison.improved_case_count == 1
    assert comparison.regressed_case_count == 1
    assert comparison.unchanged_case_count == 1
    assert comparison.total_score_delta == pytest.approx(0.0)


def _result(*scores: tuple[str, float]) -> EvaluationResult:
    metadata = ExecutionMetadata(total_tokens=0, duration_seconds=0)
    cases = tuple(
        CaseEvaluation(
            id=case_id,
            result="done",
            tracelog={},
            metadata=metadata,
            expected={},
            score=score,
            feedback="",
        )
        for case_id, score in scores
    )
    return EvaluationResult(
        total_score=sum(score for _, score in scores) / len(scores),
        cases=cases,
    )


@pytest.mark.parametrize("succeed", [False, True])
async def test_optimizer_retry_isolates_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    succeed: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """失败不污染下一次尝试，四次属于一轮且只评测成功版本。"""
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores={
            ("seed", DatasetRole.TRAIN): 0.2,
            ("seed", DatasetRole.VALIDATION): 0.2,
            ("better", DatasetRole.TRAIN): 1.0,
            ("better", DatasetRole.VALIDATION): 1.0,
        },
        changes=[],
        max_iterations=1,
    )
    requests: list[ImprovementRequest] = []
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)

    async def improve(request: ImprovementRequest) -> None:
        assert (request.workspace_dir / "prompt/system.md").read_text() == "seed\n"
        assert all(not previous.workspace_dir.exists() for previous in requests)
        requests.append(request)
        if succeed and len(requests) == 4:
            (request.workspace_dir / "prompt/system.md").write_text("better\n")
            _prepend_round(request)
            return
        (request.workspace_dir / "prompt/system.md").write_text("failed-change\n")
        message = "SECRET_TOKEN must not appear"
        raise ValueError(message)

    monkeypatch.setattr("evolution.engine.asyncio.sleep", sleep)
    monkeypatch.setattr(improver, "improve", improve)
    result = await engine.run()
    assert len(requests) == 4
    assert waits == [2, 2, 2]
    assert len({request.workspace_dir for request in requests}) == 4
    assert len({request.base_candidate_id for request in requests}) == 1
    assert all(
        request.train_evaluation == requests[0].train_evaluation for request in requests
    )
    assert result.iterations_attempted == 1
    assert result.candidates_created == (1 if succeed else 0)
    assert result.stop_reason == (
        StopReason.ITERATION_LIMIT if succeed else StopReason.IMPROVER_FAILED
    )
    assert len({request.output_candidate_id for request in requests}) == len(requests)
    failed = requests[:3] if succeed else requests
    assert all(not request.workspace_dir.exists() for request in failed)
    assert not (tmp_path / "experiment/.evolution/improver_attempts").exists()
    assert "SECRET_TOKEN" not in caplog.text
    assert len(repository.list_candidates()) == (2 if succeed else 1)
    assert len(executor.calls) == (4 if succeed else 2)


@pytest.mark.parametrize("kind", ["cancel", "cleanup"])
async def test_optimizer_retry_respects_control_and_safety_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """取消、停止不明不得重复优化或删除可能仍被访问的目录。"""
    engine, _, _, improver = _engine(
        tmp_path,
        scores={
            ("seed", DatasetRole.TRAIN): 0.2,
            ("seed", DatasetRole.VALIDATION): 0.2,
        },
        changes=[],
        max_iterations=2,
    )
    calls = 0
    workspaces: list[Path] = []

    async def improve(request: ImprovementRequest) -> None:
        nonlocal calls
        calls += 1
        workspaces.append(request.workspace_dir)
        if kind == "cancel":
            raise asyncio.CancelledError
        raise ServiceError(
            kind=ServiceErrorKind.UNAVAILABLE,
            code=f"improver_{kind}_failed",
            message="failed",
        )

    monkeypatch.setattr(improver, "improve", improve)
    if kind == "cleanup":
        result = await engine.run()
        assert result.stop_reason is StopReason.IMPROVER_FAILED
    else:
        with pytest.raises(asyncio.CancelledError):
            await engine.run()
    assert calls == 1
    assert workspaces[0].is_dir()


@pytest.mark.parametrize("error_kind", ["timeout", "runtime"])
async def test_optimizer_retries_all_ordinary_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    """超时和普通失败耗尽四次。"""
    engine, _, _, improver = _engine(
        tmp_path,
        scores={
            ("seed", DatasetRole.TRAIN): 0.2,
            ("seed", DatasetRole.VALIDATION): 0.2,
        },
        changes=[],
        max_iterations=2,
    )
    calls = 0

    async def sleep(delay: float) -> None:
        assert delay == 2

    async def improve(request: ImprovementRequest) -> None:
        nonlocal calls
        calls += 1
        if error_kind == "timeout":
            raise TimeoutError
        raise RuntimeError

    monkeypatch.setattr("evolution.engine.asyncio.sleep", sleep)
    monkeypatch.setattr(improver, "improve", improve)
    result = await engine.run()
    assert result.stop_reason is StopReason.IMPROVER_FAILED
    assert calls == 4


@pytest.mark.parametrize("boundary", ["create_workspace", "publish_workspace"])
async def test_non_optimizer_errors_do_not_restart_improver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """文件事实边界的故障不能被误判为优化异常重试。"""
    engine, repository, _, improver = _engine(
        tmp_path,
        scores={
            ("seed", DatasetRole.TRAIN): 0.2,
            ("seed", DatasetRole.VALIDATION): 0.2,
        },
        changes=[None],
        max_iterations=2,
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr(repository, boundary, fail)
    with pytest.raises(OSError):
        await engine.run()
    assert len(improver.requests) == (1 if boundary == "publish_workspace" else 0)


async def test_post_optimization_failures_do_not_retry(
    tmp_path: Path,
) -> None:
    """成功执行后的评测故障不能再次触发优化器。"""
    engine, repository, _, improver = _engine(
        tmp_path,
        scores={
            ("seed", DatasetRole.TRAIN): 0.2,
            ("seed", DatasetRole.VALIDATION): 0.2,
        },
        changes=["better"],
        max_iterations=2,
    )

    with pytest.raises(KeyError):
        await engine.run()
    assert len(improver.requests) == 1


@pytest.mark.parametrize(
    "mode", ["wrong_version", "only_backfill", "missing", "empty", "malformed"]
)
async def test_changelog_content_does_not_block_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """日志内容不参与发布决策，不触发格式重试。"""
    engine, repository, _, improver = _engine(
        tmp_path,
        scores={
            ("seed", DatasetRole.TRAIN): 0.2,
            ("seed", DatasetRole.VALIDATION): 0.2,
            ("better", DatasetRole.TRAIN): 1.0,
            ("better", DatasetRole.VALIDATION): 1.0,
        },
        changes=[],
        max_iterations=1,
    )
    seed = repository.initialize_seed()
    # 让 seed 保有一组旧记录，防止仅验证“日志非空”的实现蒙混过关。
    seed.changelog_path.chmod(0o644)
    inherited = "## 评测版本：old\n### 问题类型\n旧问题\n"
    seed.changelog_path.write_text(inherited)
    repository.save_execution(
        seed.id,
        DatasetRole.TRAIN,
        _Executor().execute(
            seed.target_dir,
            tmp_path / "train.json",
            DatasetRole.TRAIN,
        ),
    )
    repository.save_evaluation(seed.id, DatasetRole.TRAIN, _result(("train-case", 0.2)))
    repository.save_execution(
        seed.id,
        DatasetRole.VALIDATION,
        _Executor().execute(
            seed.target_dir,
            tmp_path / "validation.json",
            DatasetRole.VALIDATION,
        ),
    )
    repository.save_evaluation(
        seed.id, DatasetRole.VALIDATION, _result(("validation-case", 0.2))
    )
    requests: list[ImprovementRequest] = []

    async def improve(request: ImprovementRequest) -> None:
        assert request.changelog_path.read_text() == inherited
        requests.append(request)
        (request.workspace_dir / "prompt/system.md").write_text("better\n")
        if len(requests) == 1:
            if mode == "wrong_version":
                _prepend_round(
                    request.model_copy(update={"evidence_candidate_id": "wrong"})
                )
            elif mode in {"empty", "malformed"}:
                request.changelog_path.write_text(
                    "" if mode == "empty" else "```broken"
                )
            elif mode == "only_backfill":
                request.changelog_path.write_text(inherited.replace("旧问题", "已回填"))
            return
        _prepend_round(request)

    monkeypatch.setattr(improver, "improve", improve)
    monkeypatch.setattr("evolution.engine._IMPROVER_RETRY_DELAY_SECONDS", 0)
    result = await engine.run()
    assert len(requests) == 1
    assert result.candidates_created == 1
    published = repository.load_candidate(requests[0].output_candidate_id)
    if mode in {"empty", "malformed"}:
        assert published.changelog_path.read_text() == (
            "" if mode == "empty" else "```broken"
        )
    assert seed.changelog_path.read_text() == inherited
    assert all(not request.workspace_dir.parent.exists() for request in requests)


async def test_changelog_read_failure_does_not_retry_improver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日志读取故障保持文件错误，不被包装为格式错误重试。"""
    engine, _, _, improver = _engine(
        tmp_path,
        scores=_base_scores(),
        changes=[],
        max_iterations=1,
    )
    requests: list[ImprovementRequest] = []

    async def improve(request: ImprovementRequest) -> None:
        requests.append(request)
        (request.workspace_dir / "prompt/system.md").write_text("changed")
        request.changelog_path.unlink()

    monkeypatch.setattr(improver, "improve", improve)
    with pytest.raises(FileNotFoundError):
        await engine.run()
    assert len(requests) == 1


async def test_workspace_deletion_failure_stops_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除失败是存储故障，不继续启动优化器。"""
    engine, repository, _, improver = _engine(
        tmp_path,
        scores={
            ("seed", DatasetRole.TRAIN): 0.2,
            ("seed", DatasetRole.VALIDATION): 0.2,
        },
        changes=[],
        max_iterations=2,
    )
    requests: list[ImprovementRequest] = []

    async def improve(request: ImprovementRequest) -> None:
        requests.append(request)
        raise ValueError("optimizer failed")

    def discard(workspace: Path) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr(improver, "improve", improve)
    monkeypatch.setattr(repository, "discard_workspace", discard)
    with pytest.raises(OSError):
        await engine.run()
    assert len(requests) == 1
    assert requests[0].workspace_dir.is_dir()


async def test_sequential_chain_uses_validation_best_train_gate(tmp_path: Path) -> None:
    """父级负责继承，验证最佳负责门槛，最终最佳可以早于最新版本。"""
    rounds = [
        ("low", 0.2, None),
        ("recover", 0.3, None),
        ("tie", 0.5, 0.6),
        ("peak", 0.9, 0.6),
        ("winner", 0.6, 0.8),
        ("below", 0.55, None),
        ("validation_tie", 0.8, 0.8),
        ("next_winner", 0.7, 0.9),
        ("last", 0.2, None),
    ]
    scores = _base_scores()
    for label, train, validation in rounds:
        scores[(label, DatasetRole.TRAIN)] = train
        if validation is not None:
            scores[(label, DatasetRole.VALIDATION)] = validation
    engine, repository, executor, improver = _engine(
        tmp_path,
        scores=scores,
        changes=[label for label, _, _ in rounds],
        max_iterations=len(rounds),
        rollback_enabled=False,
    )
    result = await engine.run()
    candidates = repository.list_candidates()
    assert len(candidates) == len(rounds) + 1
    for index, child in enumerate(candidates[1:]):
        parent = candidates[index]
        request = improver.requests[index]
        assert child.parent_id == parent.id
        assert request.base_candidate_id == parent.id
        assert request.evidence_candidate_id == parent.id
        assert request.changelog_source_candidate_id == parent.id
        assert (
            improver.workspace_inputs[index]
            == (parent.target_dir / "prompt/system.md").read_text()
        )
        evaluation = repository.load_evaluation(child.id, DatasetRole.TRAIN)
        assert evaluation is not None and evaluation.parent_comparison is not None
        assert evaluation.parent_comparison.previous_candidate_id == parent.id
        assert (
            evaluation.cases[0].previous_score
            == scores[
                (
                    (parent.target_dir / "prompt/system.md").read_text().strip(),
                    DatasetRole.TRAIN,
                )
            ]
        )
    assert [
        label for label, role in executor.calls if role is DatasetRole.VALIDATION
    ] == [
        "seed",
        "tie",
        "peak",
        "winner",
        "validation_tie",
        "next_winner",
    ]
    assert result.best_candidate_id == candidates[-2].id
    assert engine._best_candidate(before_id=candidates[5].id)[0].id == candidates[0].id
    assert engine._best_candidate(before_id=candidates[8].id)[0].id == candidates[5].id
    assert result.stop_reason is StopReason.ITERATION_LIMIT
    assert result.iterations_attempted == len(rounds)


async def test_reverting_to_ancestor_keeps_sequential_parent(tmp_path: Path) -> None:
    """恢复祖先内容也是相对直接父级的一次有效修改。"""
    engine, repository, executor, _ = _engine(
        tmp_path,
        scores=_base_scores() | {("bad", DatasetRole.TRAIN): 0.2},
        changes=["bad", "seed"],
        max_iterations=2,
    )

    result = await engine.run()

    seed, bad, reverted = repository.list_candidates()
    assert reverted.parent_id == bad.id
    assert (reverted.target_dir / "prompt/system.md").read_bytes() == (
        seed.target_dir / "prompt/system.md"
    ).read_bytes()
    assert executor.calls.count(("seed", DatasetRole.TRAIN)) == 2
    assert executor.calls.count(("seed", DatasetRole.VALIDATION)) == 2
    assert result.best_candidate_id == seed.id
    assert "change-1" in reverted.changelog_path.read_text()
    assert "change-2" in reverted.changelog_path.read_text()


@pytest.mark.parametrize("seed_train,seed_validation", [(1.0, 1.0), (0.5, 0.7)])
async def test_full_scores_do_not_stop_iteration_budget(
    tmp_path: Path,
    seed_train: float,
    seed_validation: float,
) -> None:
    """Seed 或新候选满分均不缩短迭代预算。"""
    scores = {
        ("seed", DatasetRole.TRAIN): seed_train,
        ("seed", DatasetRole.VALIDATION): seed_validation,
        ("first", DatasetRole.TRAIN): 1.0,
        ("first", DatasetRole.VALIDATION): 1.0,
        ("second", DatasetRole.TRAIN): 1.0,
        ("second", DatasetRole.VALIDATION): 1.0,
    }
    engine, repository, _, improver = _engine(
        tmp_path,
        scores=scores,
        changes=["first", "second", None],
        max_iterations=3,
    )
    result = await engine.run()
    assert result.iterations_attempted == len(improver.requests) == 3
    assert result.candidates_created == 2
    assert result.stop_reason is StopReason.ITERATION_LIMIT
    candidates = repository.list_candidates()
    expected_best = candidates[0] if seed_validation == 1.0 else candidates[1]
    assert result.best_candidate_id == expected_best.id
