"""自进化 API 测试辅助：可真实运行的最小任务目录与假 Improver。

executor/evaluator 以源码字符串落盘，因为 evolution.task 按文件路径动态加载。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from evolution.improver import ImprovementRequest, Improver
from evolution.task import ResolvedTaskConfig

_CONFIG_YAML = """objective: Improve the demo agent success rate.
initial_target: ../../application/demo_agent
workspace:
  read_only: []
  inaccessible: []
max_iterations: {max_iterations}
"""

_EXECUTOR_SOURCE = '''"""测试用最小 Executor。"""

import json
import logging

from evolution.models import CaseExecution, ExecutionMetadata, ExecutionResult

_LOGGER = logging.getLogger("evolution.executor")


class _Executor:
    def execute(self, target_dir, dataset_path, dataset_role, **kwargs):
        del target_dir, kwargs
        cases = json.loads(dataset_path.read_text(encoding="utf-8"))
        results = []
        for index, case in enumerate(cases, 1):
            _LOGGER.info(
                "case_dispatched case=%s index=%s/%s completed=%s/%s active=1",
                case["id"], index, len(cases), index - 1, len(cases),
            )
            results.append(
                CaseExecution(
                    id=case["id"],
                    result="ok",
                    tracelog=None,
                    metadata=ExecutionMetadata(
                        total_tokens=10, duration_seconds=0.01
                    ),
                )
            )
            _LOGGER.info(
                "case_completed case=%s result=ok attempts=1 "
                "completed=%s/%s active=0",
                case["id"], index, len(cases),
            )
        return ExecutionResult(cases=tuple(results))


def create_executor(task_dir, settings):
    del task_dir, settings
    return _Executor()
'''

_EVALUATOR_SOURCE = '''"""测试用最小 Evaluator：全部 Case 满分。"""

from evolution.models import CaseEvaluation, EvaluationResult


class _Evaluator:
    def evaluate(self, execution, dataset_path):
        del dataset_path
        return EvaluationResult(
            total_score=1.0,
            cases=tuple(
                CaseEvaluation(
                    id=case.id,
                    result=case.result,
                    tracelog=None,
                    metadata=case.metadata,
                    expected=None,
                    score=1.0,
                    feedback="ok",
                )
                for case in execution.cases
            ),
        )


def create_evaluator(task_dir, settings):
    del task_dir, settings
    return _Evaluator()
'''


def write_demo_agent(project_root: Path, agent_dir: str = "demo_agent") -> Path:
    """创建 initial_target 指向的最小 Agent 目录。"""
    target = project_root / "application" / agent_dir
    target.mkdir(parents=True, exist_ok=True)
    (target / "prompt.txt").write_text("seed prompt", encoding="utf-8")
    return target


def write_runnable_task(
    project_root: Path,
    task_id: str,
    *,
    max_iterations: int = 1,
    with_entries: bool = True,
) -> Path:
    """创建含数据集与（可选）executor/evaluator 入口的最小任务目录。"""
    write_demo_agent(project_root)
    task_dir = project_root / "evolution_tasks" / task_id
    (task_dir / "datasets").mkdir(parents=True)
    (task_dir / "config.yaml").write_text(
        _CONFIG_YAML.format(max_iterations=max_iterations),
        encoding="utf-8",
    )
    if with_entries:
        (task_dir / "executor.py").write_text(_EXECUTOR_SOURCE, encoding="utf-8")
        (task_dir / "evaluator.py").write_text(_EVALUATOR_SOURCE, encoding="utf-8")
    for role in ("train", "validation", "test"):
        cases = [{"id": f"{role}-case-1"}, {"id": f"{role}-case-2"}]
        (task_dir / "datasets" / f"{role}.json").write_text(
            json.dumps(cases),
            encoding="utf-8",
        )
    return task_dir


class FakeImprover(AbstractAsyncContextManager[Improver]):
    """向 Workspace 写入一个净变化并维护最小 Changelog 的假 Improver。"""

    def __init__(self, *, delay_seconds: float = 0) -> None:
        self._delay_seconds = delay_seconds

    async def __aexit__(self, *exc_info: object) -> bool:
        del exc_info
        return False

    async def improve(self, request: ImprovementRequest) -> None:
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        (request.workspace_dir / "improved.txt").write_text(
            f"improved by {request.output_candidate_id}",
            encoding="utf-8",
        )
        request.changelog_path.write_text(
            f"## 评测版本：{request.base_candidate_id}\n"
            f"## 迭代版本：{request.output_candidate_id}\n",
            encoding="utf-8",
        )


def fake_improver_factory(
    improver: FakeImprover,
) -> Callable[[ResolvedTaskConfig], Awaitable[FakeImprover]]:
    """返回匹配 ImproverFactory 签名的假工厂。"""

    async def factory(config: ResolvedTaskConfig) -> FakeImprover:
        del config
        return improver

    return factory


__all__ = [
    "FakeImprover",
    "fake_improver_factory",
    "write_demo_agent",
    "write_runnable_task",
]
