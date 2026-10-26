"""评测 Run 生命周期：进程内注册表、独立线程执行与磁盘元信息状态重建。

每个 Run 在独立 daemon 线程中同步执行 ``run_evaluation``，允许全局并发
（每 Run 独立目录与 meta.json，互不干扰）。Run 状态以磁盘 meta.json 为
事实源，进程重启后仍可查询历史 Run。
"""

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agentsystem.common.errors import ServiceError
from agentsystem.common.logging import log_unexpected_exception
from evaluation.models import CaseOutcome, EvalReport
from evaluation.runner import load_dataset, run_evaluation

from server.platform.evaluation.store import EvaluationStore

_LOGGER = logging.getLogger(__name__)


class EvalRunStatus(StrEnum):
    """一次评测 Run 的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EvalRunView:
    """Run 状态的只读快照。"""

    run_id: str
    task_id: str
    status: EvalRunStatus
    created_at: float
    started_at: float | None
    finished_at: float | None
    case_count: int | None
    completed_cases: int
    final_score: float | None
    error: str | None


type RunFn = Callable[..., EvalReport]


class EvaluationRunRegistry:
    """进程内评测 Run 注册表；允许并发，状态可回落到磁盘重建。"""

    def __init__(
        self,
        *,
        store: EvaluationStore,
        environ: Mapping[str, str] | None = None,
        run_fn: RunFn | None = None,
    ) -> None:
        """绑定评测存储、环境和执行函数，初始化进程内运行注册表。

        Args:
            store: 评测任务与运行产物存储。
            environ: 用于配置环境变量插值的映射。省略时使用进程环境。
            run_fn: 可注入的评测执行函数，省略时使用 run_evaluation。
        """
        self._store = store
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )
        self._run_fn: RunFn = run_fn or run_evaluation
        self._lock = threading.Lock()
        self._runs: dict[str, EvalRunView] = {}

    def start(self, task_id: str) -> EvalRunView:
        """启动一个评测 Run；任务不存在时由 store 抛 NOT_FOUND。"""
        task_dir = self._store.task_dir(task_id)
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        run_dir = self._store.create_run_dir(task_id, run_id)
        view = EvalRunView(
            run_id=run_id,
            task_id=task_id,
            status=EvalRunStatus.PENDING,
            created_at=time.time(),
            started_at=None,
            finished_at=None,
            case_count=None,
            completed_cases=0,
            final_score=None,
            error=None,
        )
        with self._lock:
            self._runs[run_id] = view
        thread = threading.Thread(
            target=self._thread_main,
            args=(run_id, task_id, task_dir, run_dir),
            name=f"evaluation-{run_id}",
            daemon=True,
        )
        thread.start()
        return view

    def get(self, run_id: str) -> EvalRunView:
        """返回 Run 快照；内存缺失时从磁盘 meta.json 重建，不存在抛 KeyError。"""
        with self._lock:
            view = self._runs.get(run_id)
        if view is not None and view.status in (
            EvalRunStatus.PENDING,
            EvalRunStatus.RUNNING,
        ):
            return view
        try:
            _, run_dir = self._store.find_run(run_id)
        except Exception as error:
            raise KeyError(run_id) from error
        return self._view_from_meta(self._store.read_meta(run_dir))

    def list_runs(self, task_id: str) -> tuple[EvalRunView, ...]:
        """按创建时间返回指定任务的全部 Run 快照（磁盘事实源）。"""
        runs_dir = self._store.task_dir(task_id) / "runs"
        views: list[EvalRunView] = []
        if runs_dir.is_dir():
            for run_dir in sorted(runs_dir.iterdir()):
                if not run_dir.is_dir() or not (run_dir / "meta.json").is_file():
                    continue
                try:
                    views.append(self._view_from_meta(self._store.read_meta(run_dir)))
                except (ServiceError, ValueError) as error:
                    # 预期失败：损坏的 meta 记录。不静默，也不中断整个列表。
                    _LOGGER.warning(
                        "evaluation_run_skipped operation=evaluation.list_runs "
                        "run_id=%s exception_type=%s",
                        run_dir.name,
                        type(error).__name__,
                    )
                    continue
                except Exception as error:  # noqa: BLE001 - 单个损坏 Run 不影响列表
                    # 未预期异常：记录一次完整脱敏堆栈后继续。
                    log_unexpected_exception(
                        _LOGGER,
                        "evaluation_run_unexpected",
                        error,
                        operation="evaluation.list_runs",
                        run_id=run_dir.name,
                    )
                    continue
        views.sort(key=lambda view: view.created_at)
        return tuple(views)

    def _thread_main(
        self,
        run_id: str,
        task_id: str,
        task_dir: Path,
        run_dir: Path,
    ) -> None:
        started_at = time.time()
        meta = self._store.read_meta(run_dir)
        meta["status"] = EvalRunStatus.RUNNING.value
        meta["started_at"] = started_at
        try:
            case_count = len(
                load_dataset(task_dir, self._store.load_config(task_id).dataset)
            )
        except Exception:  # noqa: BLE001 - case_count 仅用于进度展示
            case_count = 0
        meta["case_count"] = case_count

        def publish() -> None:
            self._store.write_meta(run_dir, meta)
            with self._lock:
                self._runs[run_id] = self._view_from_meta(meta)

        def on_case_completed(case_id: str, outcome: CaseOutcome) -> None:
            del case_id, outcome
            completed = meta.get("completed_cases")
            meta["completed_cases"] = (
                completed if isinstance(completed, int) else 0
            ) + 1
            publish()

        publish()
        try:
            report = self._run_fn(
                task_dir,
                run_id=run_id,
                environ=self._environ,
                on_case_completed=on_case_completed,
            )
        except Exception as error:
            log_unexpected_exception(
                _LOGGER,
                "evaluation_run_failed",
                error,
                operation="evaluation.run",
                run_id=run_id,
                task_id=task_id,
            )
            meta["status"] = EvalRunStatus.FAILED.value
            meta["error"] = f"{type(error).__name__}: {error}"
            meta["finished_at"] = time.time()
            publish()
            return
        meta["status"] = EvalRunStatus.SUCCEEDED.value
        meta["final_score"] = report.final_score
        meta["completed_cases"] = report.case_count
        meta["finished_at"] = time.time()
        publish()

    def _view_from_meta(self, meta: dict[str, object]) -> EvalRunView:
        status_value = str(meta.get("status") or EvalRunStatus.PENDING.value)
        return EvalRunView(
            run_id=str(meta.get("run_id") or ""),
            task_id=str(meta.get("task_id") or ""),
            status=EvalRunStatus(status_value),
            created_at=_as_float(meta.get("created_at")),
            started_at=_as_optional_float(meta.get("started_at")),
            finished_at=_as_optional_float(meta.get("finished_at")),
            case_count=_as_optional_int(meta.get("case_count")),
            completed_cases=_as_optional_int(meta.get("completed_cases")) or 0,
            final_score=_as_optional_float(meta.get("final_score")),
            error=(str(meta["error"]) if isinstance(meta.get("error"), str) else None),
        )


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _as_optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


__all__ = ["EvalRunStatus", "EvalRunView", "EvaluationRunRegistry", "RunFn"]
