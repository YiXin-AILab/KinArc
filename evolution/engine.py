"""Seed、连续 Train 迭代、Validation 选优与阶段恢复编排。"""

import asyncio
import logging
import threading
from pathlib import Path

from agentsystem.common.errors import ServiceError
from agentsystem.common.logging import log_unexpected_exception

from evolution.candidates import (
    CandidateRecord,
    CandidateRepository,
    format_exploration_route,
    format_score_change,
    format_train_comparison,
)
from evolution.diagnosis import (
    build_diagnosis_brief,
    split_actionable_clusters,
    update_problem_library,
)
from evolution.execution import compare_evaluations
from evolution.improver import ImprovementRequest, Improver
from evolution.models import (
    DatasetRole,
    EvaluationResult,
    ExecutionResult,
    OptimizationResult,
    RollbackChangelogSource,
    RollbackReason,
    StopReason,
)
from evolution.task import TaskBundle

_LOGGER = logging.getLogger(__name__)
_IMPROVER_MAX_ATTEMPTS = 4
_IMPROVER_RETRY_DELAY_SECONDS = 2


def _log_improver_failure(
    error: Exception, workspace: Path, attempt: int, remaining: int
) -> None:
    """记录失败次数和工作目录，已记录的 Runtime 故障不重复打印堆栈。"""
    if isinstance(error, ServiceError):
        _LOGGER.warning(
            "improver_attempt_failed workspace=%s attempt=%s "
            "remaining_attempts=%s code=%s",
            workspace,
            attempt,
            remaining,
            error.code,
        )
    else:
        log_unexpected_exception(
            _LOGGER,
            "improver_attempt_failed",
            error,
            operation="evolution.improve",
            workspace=str(workspace),
            attempt=str(attempt),
            remaining_attempts=str(remaining),
        )


class EvolutionEngine:
    """在本地文件事实之上连续迭代并独立选择验证最佳版本。"""

    def __init__(
        self,
        *,
        repository: CandidateRepository,
        task: TaskBundle,
        improver: Improver,
        objective: str,
        read_only: tuple[str, ...],
        inaccessible: tuple[str, ...] = (),
        max_iterations: int,
        max_retries: int,
        retry_delay_seconds: float,
        max_case_concurrency: int = 1,
        rollback_enabled: bool = True,
        exploration_patience: int = 3,
        rollback_changelog_source: RollbackChangelogSource = "best",
        single_case_min_cases: int = 2,
    ) -> None:
        """绑定执行、评分、优化端口及候选仓库，保存本次编排策略。"""
        self._repository = repository
        self._task = task
        self._improver = improver
        self._objective = objective
        self._read_only = read_only
        self._inaccessible = inaccessible
        self._max_iterations = max_iterations
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._max_case_concurrency = max_case_concurrency
        self._rollback_enabled = rollback_enabled
        self._exploration_patience = exploration_patience
        self._rollback_changelog_source = rollback_changelog_source
        self._single_case_min_cases = single_case_min_cases

    async def run(self) -> OptimizationResult:
        """初始化或恢复实验，并在本次轮数预算内尝试改进。

        先补齐已有候选的必要评测，再依次优化、发布和评分。
        无代码变化的尝试也消耗轮次；恢复已有阶段和发布回退节点不消耗轮次。
        不自动执行 Test，也不把候选代码覆盖回初始应用。

        Returns:
            验证最佳候选、尝试轮数、新建候选数量和停止原因。

        Raises:
            ValueError: 已保存的候选关系或必需评分不完整、不一致。
            asyncio.CancelledError: 当前运行被取消；执行阶段先完成停止屏障。
        """
        _LOGGER.info(
            "run_started mode=%s max_iterations=%s",
            "resume" if self._repository.is_initialized else "initialize",
            self._max_iterations,
        )
        seed = self._repository.initialize_seed()
        await self._resume_candidates(seed)

        iterations = 0
        candidates_created = 0
        changelog_paths: list[Path] = []
        stop_reason = StopReason.ITERATION_LIMIT
        for _ in range(self._max_iterations):
            latest, evidence_train = self._latest_train_evidence()
            best, _ = self._best_candidate()
            decision = self._rollback_decision(latest, best)
            if decision is not None:
                reason, depth = decision
                latest = self._restore_candidate(latest, best, reason, depth)
                candidates_created += 1
                changelog_paths.append(latest.changelog_path)
                evidence_train = self._require_evaluation(latest.id, DatasetRole.TRAIN)
            iterations += 1
            _LOGGER.info(
                "iteration_started iteration=%s/%s candidate=%s stage=optimization",
                iterations,
                self._max_iterations,
                latest.id,
            )
            improved = await self._improve_with_retries(latest, evidence_train)
            if improved is None:
                stop_reason = StopReason.IMPROVER_FAILED
                break
            workspace_root, output_candidate_id = improved
            challenger = self._repository.publish_workspace(
                latest.id,
                workspace_root,
                candidate_id=output_candidate_id,
            )
            if challenger is None:
                _LOGGER.info(
                    "candidate_not_published output_candidate=%s parent=%s "
                    "stage=publication reason=no_change",
                    output_candidate_id,
                    latest.id,
                )
                continue
            _LOGGER.info(
                "candidate_published candidate=%s parent=%s stage=publication",
                challenger.id,
                latest.id,
            )
            changelog_paths.append(challenger.changelog_path)
            candidates_created += 1
            await self._ensure_candidate_evaluated(challenger)

        best, best_evaluation = self._best_candidate()
        _LOGGER.info(
            "run_completed best_candidate=%s validation_score=%s "
            "iterations=%s candidates_created=%s stop_reason=%s",
            best.id,
            best_evaluation.total_score,
            iterations,
            candidates_created,
            stop_reason.value,
        )
        return OptimizationResult(
            seed_candidate_id=seed.id,
            best_candidate_id=best.id,
            best_target_dir=best.target_dir,
            iterations_attempted=iterations,
            candidates_created=candidates_created,
            validation_score=best_evaluation.total_score,
            stop_reason=stop_reason,
            changelog_paths=tuple(changelog_paths),
        )

    def _rollback_decision(
        self,
        latest: CandidateRecord,
        best: CandidateRecord,
    ) -> tuple[RollbackReason, int | None] | None:
        """检查探索深度与锚点关系，返回回退原因或 None。"""
        if not self._rollback_enabled:
            return None
        train = self._require_evaluation(latest.id, DatasetRole.TRAIN)
        anchor_train = self._require_evaluation(best.id, DatasetRole.TRAIN)
        if {c.id for c in train.cases} != {c.id for c in anchor_train.cases}:
            raise ValueError("候选与锚点 Train Case 集合不一致")
        depth = self._exploration_depth(latest, best)
        if depth is None:
            return "anchor_not_in_lineage", None
        if depth >= self._exploration_patience:
            return "exploration_patience_exhausted", depth
        return None

    def _exploration_depth(
        self, latest: CandidateRecord, best: CandidateRecord
    ) -> int | None:
        """沿真实 parent 链重建深度，恢复节点是计数边界而非成功观测。"""
        digest = (best.directory / "target.sha256").read_text()
        current = latest
        depth = 0
        counting = True
        visited: set[str] = set()
        while True:
            if current.id in visited:
                raise ValueError("Candidate parent 链存在环")
            visited.add(current.id)
            if (current.directory / "target.sha256").read_text() == digest:
                return depth
            if self._repository.load_rollback(current.id) is not None:
                counting = False
            if counting:
                depth += 1
            if current.parent_id is None:
                return None
            current = self._repository.load_candidate(current.parent_id)

    def _restore_candidate(
        self,
        latest: CandidateRecord,
        best: CandidateRecord,
        reason: RollbackReason,
        depth: int | None,
    ) -> CandidateRecord:
        """发布复用最佳代码与评测事实的恢复节点，并记录其来源。"""
        restored = self._repository.publish_rollback(
            latest.id,
            best.id,
            candidate_id=self._repository.allocate_candidate_id(),
            changelog_source=self._rollback_changelog_source,
            reason=reason,
            exploration_patience=self._exploration_patience,
            exploration_depth=depth,
        )
        _LOGGER.info(
            "candidate_restored candidate=%s parent=%s source=%s "
            "reason=%s depth=%s evidence=reused",
            restored.id,
            latest.id,
            best.id,
            reason,
            depth,
        )
        return restored

    async def _improve_with_retries(
        self,
        latest: CandidateRecord,
        evidence_train: EvaluationResult,
    ) -> tuple[Path, str] | None:
        """为每次优化尝试创建独立工作区，返回成功产物或耗尽后的 None。"""
        _LOGGER.info(
            "improvement_sources code=%s changelog=%s evidence=%s",
            latest.id,
            latest.id,
            latest.id,
        )
        for attempt in range(1, _IMPROVER_MAX_ATTEMPTS + 1):
            output_candidate_id = self._repository.allocate_candidate_id()
            workspace_root = self._repository.create_workspace(
                latest.id, changelog_source_id=latest.id
            )
            request = self._improvement_request(
                latest,
                evidence_train,
                workspace_root,
                output_candidate_id,
            )
            _LOGGER.info(
                "improver_attempt_started candidate=%s stage=evolution_agent "
                "output_candidate=%s workspace=%s attempt=%s max_attempts=%s",
                latest.id,
                output_candidate_id,
                workspace_root,
                attempt,
                _IMPROVER_MAX_ATTEMPTS,
            )
            try:
                await self._improver.improve(request)
            except Exception as error:
                unsafe = (
                    isinstance(error, ServiceError)
                    and error.code == "improver_cleanup_failed"
                )
                remaining = 0 if unsafe else _IMPROVER_MAX_ATTEMPTS - attempt
                _log_improver_failure(error, workspace_root, attempt, remaining)
                if unsafe:
                    return None
            else:
                return workspace_root, output_candidate_id
            self._repository.discard_workspace(workspace_root)
            if attempt < _IMPROVER_MAX_ATTEMPTS:
                await asyncio.sleep(_IMPROVER_RETRY_DELAY_SECONDS)
        return None

    async def run_test(self, candidate_id: str) -> EvaluationResult:
        """手动执行指定候选的 Test，不参与优化、选优或父子比较。

        只有执行和评分均成功时才替换该候选已有的 Test 结果。
        """
        candidate = self._repository.load_candidate(candidate_id)
        execution = await self._execute(candidate, DatasetRole.TEST)
        evaluation = await self._evaluate(execution, DatasetRole.TEST)
        self._repository.replace_test_result(candidate.id, execution, evaluation)
        return evaluation

    async def reevaluate_test(self, candidate_id: str) -> EvaluationResult:
        """用指定候选已保存的 Test 执行重新评分，只更新 Test 评分文件。"""
        execution = self._repository.load_execution(candidate_id, DatasetRole.TEST)
        if execution is None:
            raise ValueError(
                "Test 缺少 Execution；请先运行不带 --reevaluate 的 test 命令"
            )
        evaluation = await self._evaluate(execution, DatasetRole.TEST)
        self._repository.save_evaluation(candidate_id, DatasetRole.TEST, evaluation)
        return evaluation

    async def _evaluate_candidate(
        self,
        candidate: CandidateRecord,
        role: DatasetRole,
        execution: ExecutionResult,
    ) -> EvaluationResult:
        """为尚未完成的评分阶段保存结果，并派生相对父级的比较。"""
        previous = (
            self._repository.load_evaluation(candidate.parent_id, role)
            if candidate.parent_id is not None
            else None
        )
        evaluation = _with_previous_scores(
            await self._evaluate(execution, role),
            previous,
        )
        comparison_parent = self._comparison_parent(candidate, role)
        if comparison_parent is not None:
            parent_id, parent_evaluation = comparison_parent
            evaluation = evaluation.with_comparison(
                compare_evaluations(
                    parent_id,
                    parent_evaluation,
                    candidate.id,
                    evaluation,
                )
            )
        self._repository.save_evaluation(candidate.id, role, evaluation)
        return evaluation

    async def _execute(
        self,
        candidate: CandidateRecord,
        role: DatasetRole,
    ) -> ExecutionResult:
        """在线程中执行数据集；取消时发出停止信号并等待执行者完成清理。"""
        stop = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._task.executor.execute,
                candidate.target_dir,
                self._task.datasets[role],
                role,
                max_case_concurrency=self._max_case_concurrency,
                max_retries=self._max_retries,
                retry_delay_seconds=self._retry_delay_seconds,
                cancel_event=stop,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled() and (error := worker.exception()) is not None:
                log_unexpected_exception(
                    _LOGGER,
                    "case_execution_cleanup_failed",
                    error,
                    operation="evolution.execute_cancel",
                )
            raise

    async def _evaluate(
        self,
        execution: ExecutionResult,
        role: DatasetRole,
    ) -> EvaluationResult:
        """在线程中评分已有执行事实，按角色选择数据集路径。"""
        return await asyncio.to_thread(
            self._task.evaluator.evaluate,
            execution,
            self._task.datasets[role],
        )

    def _best_candidate(
        self, *, before_id: str | None = None
    ) -> tuple[CandidateRecord, EvaluationResult]:
        """选择验证总分最高的候选；同分时选择 ID 较早者。"""
        eligible: list[tuple[float, str, CandidateRecord, EvaluationResult]] = []
        for candidate in self._repository.list_candidates():
            if before_id is not None and candidate.id >= before_id:
                continue
            evaluation = self._repository.load_evaluation(
                candidate.id,
                DatasetRole.VALIDATION,
            )
            if evaluation is not None:
                eligible.append(
                    (-evaluation.total_score, candidate.id, candidate, evaluation)
                )
        if not eligible:
            raise ValueError(
                "Experiment 缺少完整 Validation；请检查候选结果文件是否完整"
            )
        _, _, candidate, evaluation = min(eligible)
        return candidate, evaluation

    def _latest_train_evidence(self) -> tuple[CandidateRecord, EvaluationResult]:
        """按 Candidate 生成顺序选最近完整 Train，不以重评时间排序。"""
        candidates = sorted(
            self._repository.list_candidates(),
            key=lambda candidate: candidate.id,
            reverse=True,
        )
        for candidate in candidates:
            evaluation = self._repository.load_evaluation(
                candidate.id, DatasetRole.TRAIN
            )
            if evaluation is not None:
                return candidate, evaluation
        raise ValueError("Experiment 缺少完整 Train；请检查候选结果文件是否完整")

    async def _resume_candidates(self, seed: CandidateRecord) -> None:
        """校验父链顺序，再为已有候选补齐必要评测阶段。"""
        candidates = self._repository.list_candidates()
        _LOGGER.info(
            "recovery_started seed=%s latest=%s candidates=%s",
            seed.id,
            candidates[-1].id,
            len(candidates),
        )
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.id == seed.id:
                if seen or candidate.parent_id is not None:
                    raise ValueError("Seed 必须是首个且无父级的 Candidate")
            elif candidate.parent_id not in seen:
                raise ValueError(f"Candidate 父级缺失或顺序非法：{candidate.id}")
            seen.add(candidate.id)
        for candidate in candidates:
            await self._ensure_candidate_evaluated(candidate)

    async def _ensure_candidate_evaluated(
        self, candidate: CandidateRecord
    ) -> EvaluationResult:
        """恢复 Train，并按训练分数门槛决定是否验证及回填日志。"""
        if self._repository.load_rollback(candidate.id) is not None:
            self._repository.validate_rollback_results(candidate.id)
            _LOGGER.info(
                "restoration_reused candidate=%s stage=assessment", candidate.id
            )
            return self._require_evaluation(candidate.id, DatasetRole.TRAIN)
        _LOGGER.info(
            "candidate_started candidate=%s parent=%s stage=assessment",
            candidate.id,
            candidate.parent_id or "none",
        )
        train = await self._ensure_evaluation(candidate, DatasetRole.TRAIN)
        if candidate.parent_id is None:
            should_validate = True
        else:
            best, _ = self._best_candidate(before_id=candidate.id)
            best_train = self._require_evaluation(best.id, DatasetRole.TRAIN)
            should_validate = train.total_score >= best_train.total_score
        if should_validate:
            await self._ensure_evaluation(candidate, DatasetRole.VALIDATION)
        else:
            self._repository.load_execution(candidate.id, DatasetRole.VALIDATION)
            self._repository.load_evaluation(candidate.id, DatasetRole.VALIDATION)
            _LOGGER.info(
                "validation_skipped candidate=%s reason=below_best_train", candidate.id
            )
        self._backfill_changelog(candidate, train)
        return train

    def _backfill_changelog(
        self, candidate: CandidateRecord, train: EvaluationResult
    ) -> None:
        """将训练比较、验证选择和探索路线写回当前候选日志。"""
        if candidate.parent_id is None:
            return
        summary = format_train_comparison(train)
        best, best_validation = self._best_candidate(before_id=candidate.id)
        best_train = self._require_evaluation(best.id, DatasetRole.TRAIN)
        current_validation = self._repository.load_evaluation(
            candidate.id, DatasetRole.VALIDATION
        )
        relation = ">=" if train.total_score >= best_train.total_score else "<"
        gate = (
            f"验证门槛：{candidate.id} Train {train.total_score:.4f} {relation} "
            f"最佳版本 {best.id} Train {best_train.total_score:.4f}。"
        )
        adopted = (
            current_validation is not None
            and current_validation.total_score > best_validation.total_score
        )
        if current_validation is None:
            decision = "未执行 Validation，保留原最佳版本。"
        else:
            outcome = "成为新的最佳版本" if adopted else "保留原最佳版本"
            validation_change = format_score_change(
                best_validation.total_score, current_validation.total_score
            )
            decision = f"Validation：{validation_change}，{outcome}。"
        anchor = candidate if adopted else best
        route = format_exploration_route(
            rollback_enabled=self._rollback_enabled,
            depth=(
                self._exploration_depth(candidate, anchor)
                if self._rollback_enabled
                else None
            ),
            exploration_patience=self._exploration_patience,
        )
        conclusion = gate + "\n" + decision + "\n" + route
        self._repository.backfill_changelog(
            candidate.id, comparison=summary, conclusion=conclusion
        )

    async def _ensure_evaluation(
        self, candidate: CandidateRecord, role: DatasetRole
    ) -> EvaluationResult:
        """复用完整评分，或从缺失的执行或评分阶段继续并持久化。"""
        execution = self._repository.load_execution(candidate.id, role)
        evaluation = self._repository.load_evaluation(candidate.id, role)
        if evaluation is not None:
            _LOGGER.info(
                "stage_reused candidate=%s role=%s stage=evaluation reason=complete",
                candidate.id,
                role.value,
            )
            return evaluation
        if execution is None:
            _LOGGER.info(
                "stage_resuming candidate=%s role=%s stage=execution reason=missing",
                candidate.id,
                role.value,
            )
            execution = await self._execute(candidate, role)
            self._repository.save_execution(candidate.id, role, execution)
            _LOGGER.info(
                "stage_completed candidate=%s role=%s stage=execution cases=%s",
                candidate.id,
                role.value,
                len(execution.cases),
            )
        else:
            _LOGGER.info(
                "stage_reused candidate=%s role=%s stage=execution reason=complete",
                candidate.id,
                role.value,
            )
        _LOGGER.info(
            "stage_resuming candidate=%s role=%s stage=evaluation reason=missing",
            candidate.id,
            role.value,
        )
        evaluation = await self._evaluate_candidate(candidate, role, execution)
        _LOGGER.info(
            "stage_completed candidate=%s role=%s stage=evaluation score=%s cases=%s",
            candidate.id,
            role.value,
            evaluation.total_score,
            len(evaluation.cases),
        )
        return evaluation

    def _require_evaluation(
        self,
        candidate_id: str,
        role: DatasetRole,
    ) -> EvaluationResult:
        """读取必需的评分结果；缺失时以 ValueError 停止编排。"""
        evaluation = self._repository.load_evaluation(candidate_id, role)
        if evaluation is None:
            raise ValueError(
                f"Candidate 缺少 {role.value} Evaluation；请检查候选结果文件是否完整"
            )
        return evaluation

    def _comparison_parent(
        self,
        candidate: CandidateRecord,
        role: DatasetRole,
    ) -> tuple[str, EvaluationResult] | None:
        """仅为有父级的 Train 评分提供父候选及其评分。"""
        if role is not DatasetRole.TRAIN or candidate.parent_id is None:
            return None
        return (
            candidate.parent_id,
            self._require_evaluation(candidate.parent_id, DatasetRole.TRAIN),
        )

    def _improvement_request(
        self,
        latest: CandidateRecord,
        train: EvaluationResult,
        workspace_root: Path,
        output_candidate_id: str,
    ) -> ImprovementRequest:
        """更新问题库，并组装当前轮次可见的工作区和训练证据。"""
        rollback = self._repository.load_rollback(latest.id)
        brief = build_diagnosis_brief(
            latest.id,
            train,
            baseline_candidate_id=latest.parent_id,
        )
        min_cases = self._single_case_min_cases
        library = update_problem_library(
            self._repository.load_problem_library(),
            brief,
            min_cases=min_cases,
        )
        self._repository.save_problem_library(library)
        brief, deferred = split_actionable_clusters(brief, library, min_cases=min_cases)
        _LOGGER.info(
            "diagnosis_brief_built evidence=%s clusters=%s deferred=%s "
            "regressed=%s passing=%s",
            latest.id,
            len(brief.clusters),
            len(deferred),
            len(brief.regressed_cases),
            len(brief.passing_case_ids),
        )
        return ImprovementRequest(
            objective=self._objective,
            base_candidate_id=latest.id,
            changelog_source_candidate_id=latest.id,
            evidence_candidate_id=latest.id,
            output_candidate_id=output_candidate_id,
            evidence_parent_candidate_id=latest.parent_id,
            workspace_dir=workspace_root / "target",
            changelog_path=workspace_root / "changelog.md",
            read_only=self._read_only,
            inaccessible=self._inaccessible,
            train_evaluation=train,
            evidence_reused_from_candidate_id=(
                rollback.source_candidate_id if rollback else None
            ),
            rollback_changelog_source=(rollback.changelog_source if rollback else None),
            diagnosis_brief=brief,
            deferred_clusters=deferred,
            problem_library=library,
            single_case_min_cases=min_cases,
        )


def _with_previous_scores(
    current: EvaluationResult,
    previous: EvaluationResult | None,
) -> EvaluationResult:
    """按 Case ID 补充上次分数，保持当前评分与轨迹不变。"""
    if previous is None:
        return current
    previous_by_id = {case.id: case.score for case in previous.cases}
    cases = tuple(
        case.model_copy(update={"previous_score": previous_by_id.get(case.id)})
        for case in current.cases
    )
    return current.model_copy(update={"cases": cases})


__all__ = ["EvolutionEngine"]
