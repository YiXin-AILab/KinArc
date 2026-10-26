"""本地 Agent 自进化平台。"""

from evolution.models import (
    CaseEvaluation,
    CaseExecution,
    DatasetRole,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
    OptimizationResult,
    ParentComparison,
    StopReason,
)

__all__ = [
    "CaseEvaluation",
    "CaseExecution",
    "ParentComparison",
    "DatasetRole",
    "EvaluationResult",
    "ExecutionMetadata",
    "ExecutionResult",
    "OptimizationResult",
    "StopReason",
]
