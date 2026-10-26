"""Candidate 执行端口。"""

from pathlib import Path
from threading import Event
from typing import Protocol, runtime_checkable

from evolution.models import DatasetRole, ExecutionResult


@runtime_checkable
class Executor(Protocol):
    """在指定 Dataset 上运行一个不可变 Target。"""

    def execute(
        self,
        target_dir: Path,
        dataset_path: Path,
        dataset_role: DatasetRole,
        *,
        max_case_concurrency: int = 1,
        max_retries: int = 3,
        retry_delay_seconds: float = 2,
        cancel_event: Event | None = None,
    ) -> ExecutionResult:
        """解析数据并返回输入顺序的完整执行结果。

        返回前必须停止全部执行者。实现需遵守并发和重试策略，或在产生副作用前拒绝。

        Args:
            target_dir: 本次执行使用的不可变 Candidate 应用目录。
            dataset_path: 数据集文件，由具体任务解析其格式。
            dataset_role: 数据集用途，可用于选择采样策略。
            max_case_concurrency: 允许同时执行的 Case 数量。
            max_retries: 单个 Case 首次执行后的最大重试次数。
            retry_delay_seconds: 两次尝试之间的等待秒数。
            cancel_event: 可选的协作停止信号，由调用方设置。

        Returns:
            包含全部 Case 的执行事实，重试历史与消耗由实现聚合。
        """
        ...


__all__ = ["Executor"]
