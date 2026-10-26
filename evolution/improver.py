"""Candidate Workspace 改进协议。"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from agentsystem.common.models import FrozenModel
from pydantic import Field

from evolution.diagnosis import DiagnosisBrief, FailureCluster, ProblemLibrary
from evolution.models import EvaluationResult, RollbackChangelogSource


class ImprovementRequest(FrozenModel):
    """Evolution Agent 单轮可见的最小输入。

    ``base_candidate_id`` 是本轮修改起点，不表示最终验证最佳候选。
    """

    objective: str
    base_candidate_id: str
    changelog_source_candidate_id: str = Field(min_length=1)
    evidence_candidate_id: str = Field(min_length=1)
    output_candidate_id: str = Field(min_length=1)
    evidence_parent_candidate_id: str | None = Field(min_length=1)
    workspace_dir: Path
    changelog_path: Path
    read_only: tuple[str, ...]
    inaccessible: tuple[str, ...] = ()
    train_evaluation: EvaluationResult
    evidence_reused_from_candidate_id: str | None = Field(default=None, min_length=1)
    rollback_changelog_source: RollbackChangelogSource | None = None
    diagnosis_brief: DiagnosisBrief
    deferred_clusters: tuple[FailureCluster, ...] = ()
    problem_library: ProblemLibrary | None = None
    single_case_min_cases: int = Field(default=2, ge=2)


@runtime_checkable
class Improver(Protocol):
    """通过最终 Workspace 与当前 Changelog 文件交付结果。"""

    async def improve(self, request: ImprovementRequest) -> None:
        """在请求指定的工作区内完成一轮改进。

        通过工作区文件和 Changelog 交付修改，不直接发布 Candidate。
        返回或抛出普通错误前，必须停止本轮后台执行。

        Args:
            request: 包含优化目标、Train 证据和路径保护规则的单轮请求。
        """
        ...


__all__ = ["ImprovementRequest", "Improver"]
