"""Evolution 命令行入口、结果输出与实验状态展示。"""

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from unicodedata import east_asian_width

from agentsystem.common.errors import ServiceError
from agentsystem.common.logging import log_unexpected_exception, setup_logging
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import RuntimeConfig, RuntimeLoggingConfig
from dotenv import load_dotenv

from evolution.agentsystem_improver import AgentSystemImprover
from evolution.candidates import CandidateRepository, list_candidate_directories
from evolution.engine import EvolutionEngine
from evolution.improver import ImprovementRequest, Improver
from evolution.models import DatasetRole, EvaluationResult, OptimizationResult
from evolution.task import ResolvedTaskConfig, TaskBundle, load_task, load_task_config

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """创建优化、手动 Test 和只读状态命令。"""
    parser = argparse.ArgumentParser(description="运行本地 Agent 自进化实验")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="初始化或继续优化")
    _add_experiment_arguments(run)
    test = commands.add_parser("test", help="手动执行指定 Candidate 的 Test")
    test.add_argument("task_dir", type=Path)
    test.add_argument("candidate_id")
    test.add_argument(
        "--reevaluate", action="store_true", help="复用已有 Test Execution，只重新评分"
    )
    status = commands.add_parser("status", help="只读展示所有 Candidate 的评分")
    status.add_argument("task_dir", type=Path)
    return parser


def _add_experiment_arguments(parser: argparse.ArgumentParser) -> None:
    """注册运行任务及优化器 Runtime 配置的命令行参数。"""
    parser.add_argument("task_dir", type=Path)
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("config/runtime.yaml"),
    )


async def _dispatch(
    arguments: argparse.Namespace,
    engine: EvolutionEngine,
) -> OptimizationResult | EvaluationResult:
    """将已解析命令分派到优化或手动 Test 操作。"""
    if arguments.command == "run":
        return await engine.run()
    if arguments.command == "test":
        if arguments.reevaluate:
            return await engine.reevaluate_test(arguments.candidate_id)
        return await engine.run_test(arguments.candidate_id)
    raise ValueError(f"未知 Evolution 命令：{arguments.command}")


class _TestOnlyImprover:
    """手动 Test 不加载或调用进化 Agent。"""

    async def improve(self, request: ImprovementRequest) -> None:
        """拒绝在仅执行 Test 的命令中意外调用优化器。"""
        del request
        raise RuntimeError("手动 Test 不允许调用 Improver")


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    project_root: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    """执行一个 CLI 命令并返回进程退出码。"""
    arguments = build_parser().parse_args(argv)
    root = (project_root or Path.cwd()).resolve(strict=True)
    if arguments.command != "status" and environ is os.environ:
        load_dotenv(root / ".env", override=False)
    try:
        if arguments.command == "status":
            output = format_experiment_status(
                arguments.task_dir, bold=sys.stdout.isatty()
            )
        else:
            result = asyncio.run(_execute_command(arguments, root, environ))
            output = _format_result(result)
    except (OSError, ServiceError, ValueError) as error:
        print(f"evolution: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        error_id = log_unexpected_exception(
            LOGGER,
            "evolution_cli_failed",
            error,
            operation="evolution.cli",
        )
        print(f"evolution: unexpected failure ({error_id})", file=sys.stderr)
        return 1
    print(output)
    return 0


def _format_result(result: OptimizationResult | EvaluationResult) -> str:
    """序列化面向人的 CLI 摘要，完整执行证据只保留在状态仓库。"""
    if isinstance(result, EvaluationResult):
        return result.model_dump_json(
            indent=2, exclude={"cases": {"__all__": {"tracelog"}}}
        )
    return result.model_dump_json(indent=2)


async def _execute_command(
    arguments: argparse.Namespace,
    project_root: Path,
    environ: Mapping[str, str],
) -> OptimizationResult | EvaluationResult:
    """装配任务、仓库和日志，并在命令结束时关闭所持有资源。"""
    config = load_task_config(arguments.task_dir, environ=environ)
    task = load_task(config)
    repository = CandidateRepository(
        config.root,
        workspace_root=project_root / ".evolution",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=config.workspace.read_only,
        inaccessible=config.workspace.inaccessible,
    )
    if arguments.command == "test":
        logging_runtime = setup_logging(
            RuntimeLoggingConfig(file_enabled=False), project_root=project_root
        )
        try:
            engine = _new_engine(
                repository=repository,
                task=task,
                improver=_TestOnlyImprover(),
                config=config,
            )
            return await _dispatch(arguments, engine)
        finally:
            logging_runtime.close()
    runtime_path = (project_root / arguments.runtime_config).resolve(strict=True)
    runtime = load_yaml_config(runtime_path, RuntimeConfig, environ=environ)
    logging_runtime = setup_logging(runtime.logging, project_root=project_root)
    try:
        improver = await AgentSystemImprover.create(
            runtime_config=runtime,
            project_root=project_root,
            redis_prefix=_redis_prefix(config.root),
            request_timeout_seconds=config.improver_timeout_seconds,
            environ=environ,
        )
        async with improver:
            engine = _new_engine(
                repository=repository,
                task=task,
                improver=improver,
                config=config,
            )
            return await _dispatch(arguments, engine)
    finally:
        logging_runtime.close()


def _new_engine(
    *,
    repository: CandidateRepository,
    task: TaskBundle,
    improver: Improver,
    config: ResolvedTaskConfig,
) -> EvolutionEngine:
    """把已解析配置注入统一进化编排器。"""
    return EvolutionEngine(
        repository=repository,
        task=task,
        improver=improver,
        objective=config.objective,
        read_only=config.workspace.read_only,
        inaccessible=config.workspace.inaccessible,
        max_iterations=config.max_iterations,
        max_retries=config.max_retries,
        max_case_concurrency=config.max_case_concurrency,
        retry_delay_seconds=config.retry_delay_seconds,
        rollback_enabled=config.rollback_enabled,
        exploration_patience=config.exploration_patience,
        rollback_changelog_source=config.rollback_changelog_source,
        single_case_min_cases=config.single_case_min_cases,
    )


def _redis_prefix(experiment_root: Path) -> str:
    """由任务绝对路径生成稳定的优化器 Redis 命名空间。"""
    digest = hashlib.sha256(str(experiment_root).encode()).hexdigest()[:16]
    return f"evolution:{digest}"


def main() -> None:
    """Console Script 入口。"""
    raise SystemExit(run_cli())


@dataclass(frozen=True)
class _Score:
    """保存状态表所需的总分、满分题数和 Case 集合。"""

    total_score: float
    correct: int
    case_ids: frozenset[str]


def format_experiment_status(task_dir: Path, *, bold: bool = False) -> str:
    """汇总所有正式候选的保存评分，变化只与直接父版本比较。

    百分比为 Evaluator 总分；正确 Case 指 score=1，准确率为正确数/总数。
    不需要任务配置、原始代码或 Execution；本命令展示评分，不验证执行完整性。
    """
    root = task_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("任务路径必须是目录")
    candidates = list_candidate_directories(root)
    if not candidates:
        return f"任务：{root}\n候选总数：0\n暂无候选。"
    scores: dict[tuple[str, DatasetRole], _Score] = {}
    errors: set[tuple[str, DatasetRole]] = set()
    for candidate in candidates:
        for role in DatasetRole:
            key = (candidate.name, role)
            try:
                result = EvaluationResult.model_validate_json(
                    (candidate / role.value / "evaluation.json").read_text(
                        encoding="utf-8"
                    )
                )
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                errors.add(key)
                continue
            scores[key] = _Score(
                result.total_score,
                sum(case.score == 1 for case in result.cases),
                frozenset(case.id for case in result.cases),
            )
    return _format_table(root, candidates, scores, errors, bold=bold)


def _format_table(
    root: Path,
    candidates: tuple[Path, ...],
    scores: dict[tuple[str, DatasetRole], _Score],
    errors: set[tuple[str, DatasetRole]],
    *,
    bold: bool,
) -> str:
    """按候选与数据集渲染评分表，并汇总缺失或损坏结果的提示。"""
    rows = [["序号", "候选", "训练集", "验证集", "测试集"]]
    maxima = {
        role: max(
            (score.total_score for (_, r), score in scores.items() if r is role),
            default=None,
        )
        for role in DatasetRole
    }
    highlighted: set[tuple[int, int]] = set()
    notes: list[str] = []
    for index, candidate in enumerate(candidates, 1):
        try:
            parent = (candidate / "parent").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            parent = ""
        except (OSError, ValueError):
            parent = ""
            notes.append(f"{candidate.name}/parent：无法读取，变化不可用。")
        cells = []
        for column, role in enumerate(DatasetRole, 2):
            key = (candidate.name, role)
            if key in errors:
                cells.append("错误")
                notes.append(f"{candidate.name}/{role.value}：评分文件损坏或不可读。")
            else:
                cells.append(_format_score(scores.get(key), scores.get((parent, role))))
                score = scores.get(key)
                if bold and score is not None and score.total_score == maxima[role]:
                    highlighted.add((index, column))
        rows.append([str(index), candidate.name, *cells])
    best = min(
        (
            (-score.total_score, candidate_id)
            for (candidate_id, role), score in scores.items()
            if role is DatasetRole.VALIDATION
        ),
        default=None,
    )
    header = [
        f"任务：{root}",
        f"候选总数：{len(candidates)}",
        f"最新候选：{candidates[-1].name}",
        f"验证最佳：{best[1] if best else '-'}",
    ]
    legend = (
        "格式：总分%|正确Case/总数|相对父版本准确率变化"
        "（百分点，+8% 表示增加8个百分点）。\n"
        "正确Case：score=1；缺评分显示 -；无父级评分、空Case或Case集合不同时变化为 -。"
    )
    return "\n".join(
        [*header, "", *_render_rows(rows, highlighted), "", legend, *notes]
    )


def _render_rows(rows: list[list[str]], highlighted: set[tuple[int, int]]) -> list[str]:
    """先按可见文本计算列宽，再添加终端样式。"""
    widths = [max(_width(row[i]) for row in rows) for i in range(5)]
    return [
        "  ".join(
            (f"\x1b[1m{cell}\x1b[0m" if (i, j) in highlighted else cell)
            + " " * (width - _width(cell))
            for j, (cell, width) in enumerate(zip(row, widths, strict=True))
        ).rstrip()
        for i, row in enumerate(rows)
    ]


def _format_score(current: _Score | None, parent: _Score | None) -> str:
    """格式化总分、满分题数及与父级可比时的准确率变化。"""
    if current is None:
        return "-"
    count = len(current.case_ids)
    delta = "-"
    if count and parent is not None and current.case_ids == parent.case_ids:
        value = round((current.correct - parent.correct) / count * 100, 2)
        delta = "0%" if value == 0 else f"{value:+.2f}".rstrip("0").rstrip(".") + "%"
    score = f"{current.total_score * 100:.2f}".rstrip("0").rstrip(".")
    return f"{score}%|{current.correct}/{count}|{delta}"


def _width(value: str) -> int:
    """估算中英文混排文本的终端显示宽度。"""
    return sum(2 if east_asian_width(char) in "WF" else 1 for char in value)


__all__ = ["build_parser", "main", "run_cli"]
