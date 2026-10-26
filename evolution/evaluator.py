"""Candidate 评分端口。"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from evolution.models import EvaluationResult, ExecutionResult


@runtime_checkable
class Evaluator(Protocol):
    """对已完成的执行事实进行确定性评分。"""

    def evaluate(
        self,
        execution: ExecutionResult,
        dataset_path: Path,
    ) -> EvaluationResult:
        """对已有执行事实评分，不重新执行 Candidate。

        Args:
            execution: 执行器返回的 Case 结果、轨迹与消耗。
            dataset_path: 提供题目及评测依据的数据集文件。

        Returns:
            与输入 Case 集合一致的逐题评分及数据集总分。
        """
        ...


__all__ = ["Evaluator"]
