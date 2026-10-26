"""Evolution 任务磁盘存储：任务发现、模板克隆与候选状态重建。

纯文件 IO 层，无 HTTP 依赖；事实源为 ``evolution_tasks/<task>/`` 下的
``config.yaml`` 与 ``.evolution/candidates/`` 产物（与 CLI status 命令一致）。
"""

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import JsonValue
from evolution.candidates import list_candidate_directories
from evolution.models import DatasetRole
from evolution.task import TaskConfig
from pydantic import ValidationError

_TASK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CANDIDATE_ID_RE = re.compile(r"^candidate-\d{8}T\d{6}$")
_OVERRIDABLE_SCALARS = (
    "max_iterations",
    "improver_timeout_seconds",
    "max_case_concurrency",
    "max_retries",
    "rollback_enabled",
    "exploration_patience",
)


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """单个 Evolution 任务的列表级摘要。"""

    task_id: str
    objective: str
    initial_target: str
    max_iterations: int
    candidate_count: int
    best_validation_score: float | None
    best_candidate_id: str | None
    total_tokens: int
    valid: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class TemplateSummary:
    """可作为克隆来源的任务模板摘要。"""

    task_id: str
    objective: str
    initial_target: str
    dataset_case_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class TaskOverrides:
    """克隆任务时相对模板配置的覆盖字段；None 表示沿用模板。"""

    objective: str | None = None
    initial_target: str | None = None
    max_iterations: int | None = None
    improver_timeout_seconds: float | None = None
    max_case_concurrency: int | None = None
    max_retries: int | None = None
    rollback_enabled: bool | None = None
    exploration_patience: int | None = None
    settings: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class CaseScore:
    """单个 Case 的评分事实（用于通过矩阵与详情展示）。"""

    case_id: str
    score: float
    failure_kind: str | None


@dataclass(frozen=True, slots=True)
class RoleResult:
    """一个候选在某个数据集上的聚合结果与资源消耗。"""

    total_score: float
    correct: int
    case_count: int
    total_tokens: int
    duration_seconds: float
    cases: tuple[CaseScore, ...]


@dataclass(frozen=True, slots=True)
class CandidateDetail:
    """一个候选版本的完整展示事实（不含 changelog，按需单独读取）。"""

    candidate_id: str
    parent_id: str | None
    is_rollback: bool
    train: RoleResult | None
    validation: RoleResult | None
    test: RoleResult | None


@dataclass(frozen=True, slots=True)
class TaskDetail:
    """任务配置摘要与全部候选详情。"""

    task_id: str
    objective: str
    initial_target: str
    max_iterations: int
    settings: dict[str, JsonValue]
    candidates: tuple[CandidateDetail, ...]


class EvolutionStore:
    """Evolution 任务目录的唯一事实源入口。"""

    def __init__(self, *, project_root: Path, tasks_root: Path) -> None:
        """绑定项目与任务根目录，初始化候选产物的派生缓存。

        Args:
            project_root: 项目根目录，用于解析项目资源路径。
            tasks_root: 任务目录根路径。
        """
        self.project_root = project_root.resolve()
        self.tasks_root = tasks_root.resolve()
        # RoleResult 派生缓存：候选产物不可变，以 (mtime_ns, size) 失效。
        # evaluation.json 含全量 tracelog（单文件可达 10MB+），缓存避免重复解析。
        self._role_cache: dict[Path, tuple[tuple[int, int], RoleResult | None]] = {}
        self._cache_lock = threading.Lock()

    def task_dir(self, task_id: str) -> Path:
        """校验 task_id 并返回位于 tasks_root 内的任务目录。"""
        directory = self._resolve_task_dir(task_id)
        if not (directory / "config.yaml").is_file():
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="evolution_task_not_found",
                message=f"任务不存在：{task_id}",
            )
        return directory

    def list_tasks(self) -> tuple[TaskSummary, ...]:
        """列出全部任务摘要；配置损坏的任务以 valid=False 返回。"""
        if not self.tasks_root.is_dir():
            return ()
        summaries = []
        for directory in sorted(self.tasks_root.iterdir()):
            if not directory.is_dir() or not (directory / "config.yaml").is_file():
                continue
            summaries.append(self._summarize(directory))
        return tuple(summaries)

    def list_templates(self) -> tuple[TemplateSummary, ...]:
        """列出 executor/evaluator 入口可解析、可克隆的任务模板。"""
        templates = []
        for summary in self.list_tasks():
            if not summary.valid:
                continue
            directory = self.task_dir(summary.task_id)
            if not _entry_files_exist(directory, self._load_config(directory)):
                continue
            templates.append(
                TemplateSummary(
                    task_id=summary.task_id,
                    objective=summary.objective,
                    initial_target=summary.initial_target,
                    dataset_case_counts=_dataset_case_counts(directory),
                )
            )
        return tuple(templates)

    def create_task(
        self,
        *,
        template_id: str,
        task_id: str,
        overrides: TaskOverrides,
    ) -> TaskSummary:
        """从模板克隆新任务并写入校验后的 config.yaml。

        Args:
            template_id: 待克隆的任务模板标识。
            task_id: 任务标识。
            overrides: 覆盖模板配置的已校验字段。

        Returns:
            已创建任务的摘要。

        Raises:
            ServiceError: 模板不可克隆、任务 ID 冲突、目标 Agent 不存在或配置无效。
        """
        template_dir = self.task_dir(template_id)
        if not _entry_files_exist(template_dir, self._load_config(template_dir)):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_template_not_cloneable",
                message=f"模板缺少 executor/evaluator 入口：{template_id}",
            )
        target_dir = self._resolve_task_dir(task_id)
        if target_dir.exists():
            raise ServiceError(
                kind=ServiceErrorKind.CONFLICT,
                code="evolution_task_exists",
                message=f"任务已存在：{task_id}",
            )
        config = self._merge_config(template_dir, target_dir, overrides)
        shutil.copytree(
            template_dir,
            target_dir,
            ignore=shutil.ignore_patterns(".evolution", "__pycache__"),
        )
        try:
            (target_dir / "config.yaml").write_text(
                yaml.safe_dump(
                    config.model_dump(mode="json"),
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
        return self._summarize(target_dir)

    def task_detail(self, task_id: str) -> TaskDetail:
        """重建任务配置摘要与全部候选详情（不含 changelog 原文）。"""
        directory = self.task_dir(task_id)
        config = self._load_config(directory)
        candidates = tuple(
            self._candidate_detail(candidate)
            for candidate in list_candidate_directories(directory)
        )
        return TaskDetail(
            task_id=directory.name,
            objective=config.objective,
            initial_target=str(config.initial_target),
            max_iterations=config.max_iterations,
            settings=dict(config.settings),
            candidates=candidates,
        )

    def changelog(self, task_id: str, candidate_id: str) -> str:
        """按需读取单个候选的 changelog 原文（内容可能很大，不进列表/详情）。

        Args:
            task_id: 任务标识。
            candidate_id: 候选版本标识。

        Returns:
            候选版本的 changelog 原文。
        """
        directory = self.task_dir(task_id)
        if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_candidate_id_invalid",
                message=f"候选 ID 不合法：{candidate_id}",
            )
        path = (
            directory / ".evolution" / "candidates" / candidate_id / "changelog.md"
        ).resolve()
        try:
            path.relative_to(directory)
        except ValueError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_candidate_id_invalid",
                message=f"候选 ID 越界：{candidate_id}",
            ) from error
        text = _read_text_or_none(path)
        if text is None:
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="evolution_changelog_not_found",
                message=f"候选 changelog 不存在：{candidate_id}",
            )
        return text

    def _resolve_task_dir(self, task_id: str) -> Path:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_task_id_invalid",
                message=f"任务 ID 不合法：{task_id}",
            )
        directory = (self.tasks_root / task_id).resolve()
        try:
            directory.relative_to(self.tasks_root)
        except ValueError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_task_id_invalid",
                message=f"任务 ID 越界：{task_id}",
            ) from error
        return directory

    def _merge_config(
        self,
        template_dir: Path,
        target_dir: Path,
        overrides: TaskOverrides,
    ) -> TaskConfig:
        merged = yaml.safe_load(
            (template_dir / "config.yaml").read_text(encoding="utf-8")
        )
        if not isinstance(merged, dict):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_task_config_invalid",
                message=f"模板配置无效：{template_dir.name}",
            )
        merged = dict(merged)
        if overrides.objective is not None:
            merged["objective"] = overrides.objective
        for field in _OVERRIDABLE_SCALARS:
            value = getattr(overrides, field)
            if value is not None:
                merged[field] = value
        if overrides.initial_target is not None:
            merged["initial_target"] = self._resolve_initial_target(
                overrides.initial_target, target_dir
            )
        if overrides.settings:
            settings = dict(merged.get("settings") or {})
            settings.update(overrides.settings)
            merged["settings"] = settings
        try:
            return TaskConfig.model_validate(merged)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
                for item in error.errors()
            )
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_task_config_invalid",
                message=f"任务配置无效：{details}",
            ) from error

    def _resolve_initial_target(self, value: str, target_dir: Path) -> str:
        candidate = (self.project_root / value).resolve()
        try:
            candidate.relative_to(self.project_root)
        except ValueError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_initial_target_invalid",
                message=f"initial_target 必须位于项目根目录内：{value}",
            ) from error
        if not candidate.is_dir():
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_initial_target_invalid",
                message=f"initial_target 目录不存在：{value}",
            )
        return os.path.relpath(candidate, target_dir)

    def _summarize(self, directory: Path) -> TaskSummary:
        try:
            config = self._load_config(directory)
        except ServiceError as error:
            return TaskSummary(
                task_id=directory.name,
                objective="",
                initial_target="",
                max_iterations=0,
                candidate_count=0,
                best_validation_score=None,
                best_candidate_id=None,
                total_tokens=0,
                valid=False,
                error=error.message,
            )
        candidates = list_candidate_directories(directory)
        best_id: str | None = None
        best_score: float | None = None
        total_tokens = 0
        for candidate in candidates:
            detail = self._candidate_detail(candidate)
            for role in (detail.train, detail.validation, detail.test):
                if role is not None:
                    total_tokens += role.total_tokens
            if detail.validation is not None and (
                best_score is None or detail.validation.total_score > best_score
            ):
                best_score = detail.validation.total_score
                best_id = detail.candidate_id
        return TaskSummary(
            task_id=directory.name,
            objective=config.objective,
            initial_target=str(config.initial_target),
            max_iterations=config.max_iterations,
            candidate_count=len(candidates),
            best_validation_score=best_score,
            best_candidate_id=best_id,
            total_tokens=total_tokens,
            valid=True,
            error=None,
        )

    def _load_config(self, directory: Path) -> TaskConfig:
        try:
            raw = yaml.safe_load(
                (directory / "config.yaml").read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_task_config_unreadable",
                message=f"任务配置不可读：{directory.name}",
            ) from error
        try:
            return TaskConfig.model_validate(raw)
        except ValidationError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="evolution_task_config_invalid",
                message=(
                    f"任务配置无效：{directory.name}（{error.error_count()} 个错误）"
                ),
            ) from error

    def _candidate_detail(self, candidate: Path) -> CandidateDetail:
        parent = _read_text_or_none(candidate / "parent")
        return CandidateDetail(
            candidate_id=candidate.name,
            parent_id=parent.strip() if parent else None,
            is_rollback=(candidate / "rollback.json").is_file(),
            train=self._role_result(candidate, DatasetRole.TRAIN),
            validation=self._role_result(candidate, DatasetRole.VALIDATION),
            test=self._role_result(candidate, DatasetRole.TEST),
        )

    def _role_result(self, candidate: Path, role: DatasetRole) -> RoleResult | None:
        """从 evaluation.json 轻量提取展示字段（跳过 60MB+ 的 execution.json）。

        用 stdlib json 代替 pydantic 全量校验，并以 (mtime_ns, size) 缓存派生结果；
        tokens/duration 取评分阶段的 case metadata 作为资源消耗口径。
        """
        evaluation_path = candidate / role.value / "evaluation.json"
        if not evaluation_path.is_file():
            return None
        try:
            stat = evaluation_path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._cache_lock:
            hit = self._role_cache.get(evaluation_path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        result = _parse_role_result(evaluation_path)
        with self._cache_lock:
            self._role_cache[evaluation_path] = (stamp, result)
        return result


def _entry_files_exist(directory: Path, config: TaskConfig) -> bool:
    """按配置声明解析 executor/evaluator 入口（允许指向任务目录外）。"""
    return (directory / config.executor).is_file() and (
        directory / config.evaluator
    ).is_file()


def _dataset_case_counts(directory: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for role in DatasetRole:
        path = directory / "datasets" / f"{role.value}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            counts[role.value] = len(data)
    return counts


def _parse_role_result(evaluation_path: Path) -> RoleResult | None:
    try:
        raw = json.loads(evaluation_path.read_text(encoding="utf-8"))
        cases_raw = raw["cases"]
        total_score = float(raw["total_score"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    cases = []
    total_tokens = 0
    duration = 0.0
    for case in cases_raw:
        try:
            metadata = case.get("metadata") or {}
            score = float(case["score"])
            total_tokens += int(metadata.get("total_tokens") or 0)
            duration += float(metadata.get("duration_seconds") or 0.0)
            cases.append(
                CaseScore(
                    case_id=str(case["id"]),
                    score=score,
                    failure_kind=case.get("failure_kind"),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return RoleResult(
        total_score=total_score,
        correct=sum(case.score == 1 for case in cases),
        case_count=len(cases),
        total_tokens=total_tokens,
        duration_seconds=duration,
        cases=tuple(cases),
    )


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


__all__ = [
    "CandidateDetail",
    "CaseScore",
    "EvolutionStore",
    "RoleResult",
    "TaskDetail",
    "TaskOverrides",
    "TaskSummary",
    "TemplateSummary",
]
