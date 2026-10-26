"""Current Evolution ports; all benchmark implementation lives inside the task."""

import json
import math
from collections.abc import Mapping
from pathlib import Path
from threading import Event

from agentsystem.common.models import JsonValue
from evolution.models import DatasetRole, EvaluationResult, ExecutionResult

from .portable import check_dataset
from .process import invoke


class Executor:
    """Execute using the pinned task worker instead of importing its old Runtime."""

    def __init__(self, root: Path, settings: Mapping[str, JsonValue]) -> None:
        self.root = root.resolve()
        if settings != {"portable": True}:
            raise ValueError("Portable task settings must be exactly {portable: true}")

    def execute(
        self,
        target_dir: Path,
        dataset_path: Path,
        dataset_role: DatasetRole,
        *,
        max_case_concurrency: int = 1,
        max_retries: int = 0,
        retry_delay_seconds: float = 2,
        cancel_event: Event | None = None,
    ) -> ExecutionResult:
        if max_retries != 0:
            raise ValueError(
                "Portable EAB uses cumulative deadlines, not case resampling"
            )
        if not 1 <= max_case_concurrency <= 16:
            raise ValueError("EAB concurrency must be between 1 and 16")
        ids = check_dataset(dataset_path)
        value = invoke(
            self.root,
            {
                "operation": "execute",
                "target": str(target_dir.resolve()),
                "dataset": str(dataset_path.resolve()),
                "role": dataset_role.value,
                "concurrency": max_case_concurrency,
            },
            timeout=math.ceil(len(ids) / max_case_concurrency) * 1200 + 300,
            cancel=cancel_event,
        )
        return ExecutionResult.model_validate(value)


class Evaluator:
    """Aggregate existing verified receipts with the original scoring code."""

    def __init__(self, root: Path, settings: Mapping[str, JsonValue]) -> None:
        self.root = root.resolve()
        if settings != {"portable": True}:
            raise ValueError("Portable task settings must be exactly {portable: true}")

    def evaluate(
        self, execution: ExecutionResult, dataset_path: Path
    ) -> EvaluationResult:
        check_dataset(dataset_path)
        dataset_role = DatasetRole(json.loads(dataset_path.read_text())["role"])
        value = invoke(
            self.root,
            {
                "operation": "evaluate",
                "execution": execution.model_dump(mode="json"),
                "dataset": str(dataset_path.resolve()),
                "role": dataset_role.value,
            },
            timeout=600,
        )
        if "challenge_comparison" in value:
            value["parent_comparison"] = value.pop("challenge_comparison")
        return EvaluationResult.model_validate(value)
