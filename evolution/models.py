"""Evolution 执行、评分、比较与优化结果模型。"""

from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from agentsystem.common.models import FrozenModel, JsonValue
from pydantic import Field, model_validator


class DatasetRole(StrEnum):
    """Dataset 在进化流程中的固定用途。"""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class StopReason(StrEnum):
    """一次优化流程的稳定停止原因。"""

    ITERATION_LIMIT = "iteration_limit"
    USER_STOP = "user_stop"
    IMPROVER_FAILED = "improver_failed"


RollbackChangelogSource = Literal["latest", "best"]
RollbackReason = Literal["exploration_patience_exhausted", "anchor_not_in_lineage"]


class RollbackArtifactHashes(FrozenModel):
    """首次恢复时捕获的来源结果摘要。"""

    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RollbackRecord(FrozenModel):
    """恢复发布的不可变来源事实，不维护动态复用状态。"""

    source_candidate_id: str = Field(pattern=r"^candidate-\d{8}T\d{6}$")
    changelog_source: RollbackChangelogSource
    changelog_source_candidate_id: str = Field(pattern=r"^candidate-\d{8}T\d{6}$")
    reason: RollbackReason
    exploration_patience: int = Field(ge=1, strict=True)
    exploration_depth: int | None = Field(default=None, ge=0, strict=True)
    source_target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reused_artifacts: dict[DatasetRole, RollbackArtifactHashes]

    @model_validator(mode="after")
    def validate_restoration(self) -> Self:
        """恢复必须承诺完整 Train/Validation，并给出一致的原因和深度。"""
        if set(self.reused_artifacts) != {DatasetRole.TRAIN, DatasetRole.VALIDATION}:
            raise ValueError("恢复来源必须恰好包含 Train 和 Validation")
        if self.reason == "anchor_not_in_lineage":
            if self.exploration_depth is not None:
                raise ValueError("锚点不在链中时探索深度必须为空")
        elif (
            self.exploration_depth is None
            or self.exploration_depth < self.exploration_patience
        ):
            raise ValueError("探索深度必须达到回退阈值")
        return self


class ExecutionMetadata(FrozenModel):
    """单 Case 的资源消耗。"""

    total_tokens: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)


class CaseAttempt(FrozenModel):
    """单 Case 一次独立执行的完整事实。"""

    result: str
    tracelog: JsonValue
    metadata: ExecutionMetadata


class CaseExecution(FrozenModel):
    """Executor 产生的单 Case 执行事实。"""

    id: str = Field(min_length=1)
    result: str
    tracelog: JsonValue
    metadata: ExecutionMetadata
    retryable: bool = False
    attempts: tuple[CaseAttempt, ...] = ()
    trials: tuple[CaseAttempt, ...] = ()

    @model_validator(mode="after")
    def validate_attempt_history(self) -> Self:
        """发生 retry 时校验最终事实和累计资源。"""
        if not self.attempts:
            return self
        if len(self.attempts) < 2:
            raise ValueError("重试历史至少包含两次 Attempt")
        final = self.attempts[-1]
        if final.result != self.result or final.tracelog != self.tracelog:
            raise ValueError("Case 顶层结果必须匹配最终 Attempt")
        total_tokens = sum(item.metadata.total_tokens for item in self.attempts)
        duration_seconds = sum(item.metadata.duration_seconds for item in self.attempts)
        if (
            self.metadata.total_tokens != total_tokens
            or self.metadata.duration_seconds != duration_seconds
        ):
            raise ValueError("Case 顶层 metadata 必须等于所有 Attempts 之和")
        return self

    @model_validator(mode="after")
    def validate_trials(self) -> Self:
        """独立重复采样时校验代表事实和累计资源。"""
        if not self.trials:
            return self
        if self.attempts:
            raise ValueError("Trials 与 Attempts 语义互斥，不得同时非空")
        if len(self.trials) < 2:
            raise ValueError("重复采样至少包含两次 Trial")
        representative = self.trials[0]
        if (
            representative.result != self.result
            or representative.tracelog != self.tracelog
        ):
            raise ValueError("Case 顶层结果必须匹配首个 Trial")
        total_tokens = sum(item.metadata.total_tokens for item in self.trials)
        duration_seconds = sum(item.metadata.duration_seconds for item in self.trials)
        if (
            self.metadata.total_tokens != total_tokens
            or self.metadata.duration_seconds != duration_seconds
        ):
            raise ValueError("Case 顶层 metadata 必须等于所有 Trials 之和")
        return self


class ExecutionResult(FrozenModel):
    """一个 Candidate 在一个 Dataset 上的执行结果。"""

    cases: tuple[CaseExecution, ...]

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> Self:
        """拒绝无法唯一定位的 Case 执行事实。"""
        _require_unique_ids(tuple(case.id for case in self.cases))
        return self


class ParentComparison(FrozenModel):
    """当前 Candidate 与父 Candidate 的 Train 比较。"""

    previous_candidate_id: str = Field(min_length=1)
    current_candidate_id: str = Field(min_length=1)
    previous_total_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    current_total_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    total_score_delta: float
    previous_case_count: int = Field(ge=0)
    current_case_count: int = Field(ge=0)
    improved_case_count: int | None = Field(default=None, ge=0)
    regressed_case_count: int | None = Field(default=None, ge=0)
    unchanged_case_count: int | None = Field(default=None, ge=0)


class CaseEvaluation(FrozenModel):
    """Evaluator 产生的单 Case 评分事实。

    failure_kind 是 Evaluator 可选提供的 Case 不变失败类别（如
    action_mismatch:<tool_name>），诊断聚类优先于 feedback 文本启发式使用。
    """

    id: str = Field(min_length=1)
    result: str
    tracelog: JsonValue
    metadata: ExecutionMetadata
    expected: JsonValue
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    previous_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    feedback: str
    failure_kind: str | None = Field(default=None, min_length=1)


class EvaluationResult(FrozenModel):
    """一个 Candidate 在一个 Dataset 上的聚合评分。"""

    total_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    cases: tuple[CaseEvaluation, ...]
    parent_comparison: ParentComparison | None = None

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> Self:
        """拒绝无法唯一定位的 Case 评分事实。"""
        _require_unique_ids(tuple(case.id for case in self.cases))
        return self

    def with_comparison(
        self,
        comparison: ParentComparison,
    ) -> "EvaluationResult":
        """返回附带父子 Train 比较的新评分事实。"""
        return self.model_copy(update={"parent_comparison": comparison})


class FileChange(FrozenModel):
    """父 Target 与 Workspace 之间的一项净变化。"""

    path: str
    kind: Literal["added", "modified", "deleted"]
    diff: str | None = None
    before_sha256: str | None = None
    after_sha256: str | None = None
    before_bytes: int | None = None
    after_bytes: int | None = None


class OptimizationResult(FrozenModel):
    """一次核心进化流程的最终输出。"""

    seed_candidate_id: str = Field(min_length=1)
    best_candidate_id: str = Field(min_length=1)
    best_target_dir: Path
    iterations_attempted: int = Field(ge=0)
    candidates_created: int = Field(ge=0)
    validation_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    stop_reason: StopReason
    changelog_paths: tuple[Path, ...] = ()


def _require_unique_ids(case_ids: tuple[str, ...]) -> None:
    """拒绝结果中出现重复的 Case ID。"""
    duplicates = sorted(
        case_id for case_id, count in Counter(case_ids).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"Case ID 重复：{', '.join(duplicates)}")


__all__ = [
    "CaseAttempt",
    "CaseEvaluation",
    "CaseExecution",
    "ParentComparison",
    "DatasetRole",
    "EvaluationResult",
    "ExecutionMetadata",
    "ExecutionResult",
    "FileChange",
    "OptimizationResult",
    "RollbackArtifactHashes",
    "RollbackChangelogSource",
    "RollbackReason",
    "RollbackRecord",
    "StopReason",
]
