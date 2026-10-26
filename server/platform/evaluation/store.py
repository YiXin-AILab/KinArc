"""评测任务磁盘存储：任务发现、Run 元信息与报告重建。

纯文件 IO 层，无 HTTP 依赖；事实源为 ``eval_tasks/<task_id>/`` 下的
``config.yaml``、``datasets/`` 与 ``runs/<run_id>/`` 产物。
"""

import json
import re
import time
from pathlib import Path

import yaml
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from evaluation.models import EvalReport, EvalTaskConfig
from evaluation.runner import load_dataset
from pydantic import ValidationError

_TASK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class EvaluationStore:
    """评测任务目录的唯一事实源入口。"""

    def __init__(self, *, tasks_root: Path) -> None:
        """绑定评测任务根目录，不创建任务或运行产物。

        Args:
            tasks_root: 任务目录根路径。
        """
        self.tasks_root = tasks_root.resolve()

    def task_dir(self, task_id: str) -> Path:
        """校验 task_id 并返回任务目录；不存在时抛 NOT_FOUND。"""
        directory = self._resolve_task_dir(task_id)
        if not (directory / "config.yaml").is_file():
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="evaluation_task_not_found",
                message=f"评测任务不存在：{task_id}",
            )
        return directory

    def list_tasks(self) -> tuple[dict[str, object], ...]:
        """列出全部任务摘要；配置或数据集损坏的任务以 valid=False 返回。"""
        if not self.tasks_root.is_dir():
            return ()
        summaries = []
        for directory in sorted(self.tasks_root.iterdir()):
            if not directory.is_dir() or not (directory / "config.yaml").is_file():
                continue
            summaries.append(self._summarize(directory))
        return tuple(summaries)

    def load_config(self, task_id: str) -> EvalTaskConfig:
        """加载并严格校验磁盘任务配置，不进行环境变量插值。"""
        directory = self.task_dir(task_id)
        try:
            raw = yaml.safe_load(
                (directory / "config.yaml").read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_task_config_unreadable",
                message=f"任务配置不可读：{task_id}",
            ) from error
        try:
            return EvalTaskConfig.model_validate(raw)
        except ValidationError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_task_config_invalid",
                message=(f"任务配置无效：{task_id}（{error.error_count()} 个错误）"),
            ) from error

    def create_run_dir(self, task_id: str, run_id: str) -> Path:
        """创建 Run 目录并写入初始 pending 元信息。

        Args:
            task_id: 任务标识。
            run_id: 运行标识。

        Returns:
            已写入初始元信息的运行目录。
        """
        run_dir = self.task_dir(task_id) / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self.write_meta(
            run_dir,
            {
                "run_id": run_id,
                "task_id": task_id,
                "status": "pending",
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "case_count": None,
                "completed_cases": 0,
                "final_score": None,
                "error": None,
            },
        )
        return run_dir

    def write_meta(self, run_dir: Path, meta: dict[str, object]) -> None:
        """覆盖写入 Run 元信息（不含任何密钥材料）。

        Args:
            run_dir: 运行产物目录。
            meta: 要写入的运行元信息，不应包含密钥。
        """
        (run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def find_run(self, run_id: str) -> tuple[str, Path]:
        """按 run_id 在全部任务中定位 Run 目录；不存在时抛 NOT_FOUND。"""
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_run_id_invalid",
                message=f"Run ID 不合法：{run_id}",
            )
        if self.tasks_root.is_dir():
            for task_dir in sorted(self.tasks_root.iterdir()):
                run_dir = task_dir / "runs" / run_id
                if task_dir.is_dir() and (run_dir / "meta.json").is_file():
                    return task_dir.name, run_dir
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="evaluation_run_not_found",
            message=f"Run 不存在：{run_id}",
        )

    def read_meta(self, run_dir: Path) -> dict[str, object]:
        """读取 Run 元信息；损坏时抛 INVALID。"""
        try:
            raw = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_run_meta_unreadable",
                message=f"Run 元信息不可读：{run_dir.name}",
            ) from error
        if not isinstance(raw, dict):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_run_meta_unreadable",
                message=f"Run 元信息格式无效：{run_dir.name}",
            )
        return raw

    def read_report(self, run_id: str) -> EvalReport:
        """读取已完成 Run 的报告；缺失或损坏时抛对应 ServiceError。"""
        task_id, run_dir = self.find_run(run_id)
        del task_id
        path = run_dir / "report.json"
        if not path.is_file():
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="evaluation_report_not_found",
                message=f"Run 报告不存在（Run 可能未完成）：{run_id}",
            )
        try:
            return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_report_unreadable",
                message=f"Run 报告不可读：{run_id}",
            ) from error

    def _resolve_task_dir(self, task_id: str) -> Path:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_task_id_invalid",
                message=f"任务 ID 不合法：{task_id}",
            )
        directory = (self.tasks_root / task_id).resolve()
        try:
            directory.relative_to(self.tasks_root)
        except ValueError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evaluation_task_id_invalid",
                message=f"任务 ID 越界：{task_id}",
            ) from error
        return directory

    def _summarize(self, directory: Path) -> dict[str, object]:
        task_id = directory.name
        try:
            config = self.load_config(task_id)
            case_count = len(load_dataset(directory, config.dataset))
        except ServiceError as error:
            return {
                "task_id": task_id,
                "agent_id": "",
                "dataset": "",
                "case_count": 0,
                "run_count": 0,
                "valid": False,
                "error": error.message,
            }
        except (OSError, ValueError) as error:
            return {
                "task_id": task_id,
                "agent_id": config.agent_id,
                "dataset": config.dataset,
                "case_count": 0,
                "run_count": 0,
                "valid": False,
                "error": f"数据集无效：{error}",
            }
        runs_dir = directory / "runs"
        run_count = (
            sum(
                1
                for item in runs_dir.iterdir()
                if item.is_dir() and (item / "meta.json").is_file()
            )
            if runs_dir.is_dir()
            else 0
        )
        return {
            "task_id": task_id,
            "agent_id": config.agent_id,
            "dataset": config.dataset,
            "case_count": case_count,
            "run_count": run_count,
            "valid": True,
            "error": None,
        }


__all__ = ["EvaluationStore"]
