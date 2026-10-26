"""候选快照的发布、恢复、持久化及文件与 Changelog 处理。"""

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from agentsystem.common.models import FrozenModel, JsonValue

from evolution.diagnosis import ProblemLibrary
from evolution.execution import compare_evaluations, format_evaluation_json
from evolution.models import (
    DatasetRole,
    EvaluationResult,
    ExecutionResult,
    FileChange,
    RollbackArtifactHashes,
    RollbackChangelogSource,
    RollbackReason,
    RollbackRecord,
)
from evolution.workspace import WorkspaceAccess

_CANDIDATE_PATTERN = re.compile(r"candidate-\d{8}T\d{6}")
_LOGGER = logging.getLogger(__name__)


class ExperimentManifest(FrozenModel):
    """Experiment 初始化时展示的配置事实。"""

    seed_candidate_id: str
    objective: str
    read_only: tuple[str, ...]
    inaccessible: tuple[str, ...] = ()


class CandidateRecord(FrozenModel):
    """已发布 Candidate 的代码与日志定位。"""

    id: str
    parent_id: str | None
    directory: Path
    target_dir: Path
    changelog_path: Path


@dataclass(frozen=True)
class _ResultSnapshot:
    """保存执行与评分的原始字节及校验后的模型，供回退复用。"""

    execution_bytes: bytes
    evaluation_bytes: bytes
    execution: ExecutionResult
    evaluation: EvaluationResult


class CandidateRepository:
    """以本地目录为唯一权威的 Candidate 仓库。"""

    def __init__(
        self,
        experiment_root: Path,
        *,
        workspace_root: Path,
        initial_target: Path,
        objective: str,
        read_only: tuple[str, ...],
        inaccessible: tuple[str, ...] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定实验目录、初始目标和保护策略；此时不创建候选。"""
        self._experiment_root = experiment_root.resolve()
        self.initial_target = initial_target.resolve(strict=True)
        self._objective = objective
        self._read_only = read_only
        self._inaccessible = inaccessible
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_candidate_time: datetime | None = None
        self._root = self._experiment_root / ".evolution"
        self._candidates = self._root / "candidates"
        experiment_key = hashlib.sha256(str(self._experiment_root).encode()).hexdigest()
        self._workspaces = workspace_root.resolve()
        self._workspace_prefix = f".workspace-{experiment_key[:16]}-"
        self._manifest_path = self._root / "manifest.json"

    @property
    def is_initialized(self) -> bool:
        """返回不可变 Manifest 是否已经发布。"""
        return self._manifest_path.is_file()

    def initialize_seed(self) -> CandidateRecord:
        """导入 initial_target；重复打开时返回并校验已有 Seed。"""
        if self._manifest_path.exists():
            manifest = self._load_manifest()
            return self.load_candidate(manifest.seed_candidate_id)
        self._candidates.mkdir(parents=True, exist_ok=True)
        self._workspaces.mkdir(parents=True, exist_ok=True)
        existing = self.list_candidates()
        if existing:
            if len(existing) != 1 or existing[0].parent_id is not None:
                raise ValueError("缺少 Manifest，无法确定唯一 Seed")
            seed = existing[0]
            self._save_manifest(seed.id)
            return seed
        candidate_id = self.allocate_candidate_id()
        staging = self._candidates / f".{candidate_id}.staging"
        directory = self._candidates / candidate_id
        self._write_candidate(
            staging,
            source=self.initial_target,
            parent_id=None,
            changelog_source=None,
        )
        os.replace(staging, directory)
        try:
            self._save_manifest(candidate_id)
        except BaseException:
            shutil.rmtree(directory)
            raise
        return self.load_candidate(candidate_id)

    def _save_manifest(self, seed_candidate_id: str) -> None:
        """原子记录 Seed、优化目标和工作区保护范围。"""
        manifest = ExperimentManifest(
            seed_candidate_id=seed_candidate_id,
            objective=self._objective,
            read_only=self._read_only,
            inaccessible=self._inaccessible,
        )
        atomic_write_json(self._manifest_path, manifest.model_dump(mode="json"))

    def create_workspace(self, parent_id: str, *, changelog_source_id: str) -> Path:
        """分别从代码父级和累计日志来源创建可写 Workspace。"""
        parent = self.load_candidate(parent_id)
        changelog_source = self.load_candidate(changelog_source_id)
        self._workspaces.mkdir(parents=True, exist_ok=True)
        workspace_root = (
            self._workspaces / f"{self._workspace_prefix}{parent_id}-{uuid4().hex}"
        )
        workspace_root.mkdir()
        try:
            copy_tree(parent.target_dir, workspace_root / "target")
            shutil.copyfile(
                changelog_source.changelog_path, workspace_root / "changelog.md"
            )
        except BaseException:
            shutil.rmtree(workspace_root)
            raise
        return workspace_root

    def discard_workspace(self, workspace_root: Path) -> None:
        """删除已确认停止的失败副本，仅允许本实验的直接 Workspace 子目录。"""
        root = self._workspaces.resolve(strict=True)
        resolved = workspace_root.resolve(strict=True)
        if (
            workspace_root.is_symlink()
            or resolved.parent != root
            or not resolved.name.startswith(self._workspace_prefix)
        ):
            raise ValueError("清理路径不属于当前 Experiment Workspace")
        shutil.rmtree(resolved)

    def publish_workspace(
        self,
        parent_id: str,
        workspace_root: Path,
        *,
        candidate_id: str,
    ) -> CandidateRecord | None:
        """校验 Workspace 净变化后原子发布正式 Candidate。"""
        parent = self.load_candidate(parent_id)
        resolved_workspace = workspace_root.resolve(strict=True)
        if (
            workspace_root.is_symlink()
            or resolved_workspace.parent != self._workspaces.resolve(strict=True)
            or not resolved_workspace.name.startswith(self._workspace_prefix)
        ):
            raise ValueError("Workspace 不属于当前 Experiment")
        target = resolved_workspace / "target"
        policy = WorkspaceAccess(
            read_only=self._read_only, inaccessible=self._inaccessible
        )
        before_dirs = {
            p.relative_to(parent.target_dir).as_posix()
            for p in parent.target_dir.rglob("*")
            if p.is_dir()
        }
        after_dirs = {
            p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_dir()
        }
        if any(policy.is_restricted(p) for p in before_dirs ^ after_dirs):
            raise ValueError("变化影响工作区权限边界")
        changes = collect_changes(parent.target_dir, target)
        if not changes:
            shutil.rmtree(resolved_workspace)
            return None
        invalid = tuple(
            change.path for change in changes if policy.is_restricted(change.path)
        )
        if invalid:
            raise ValueError("变化影响 read_only 或不可访问边界")
        staging = self._candidates / f".{candidate_id}.staging"
        directory = self._candidate_directory(candidate_id)
        if os.path.lexists(directory) or os.path.lexists(staging):
            raise FileExistsError(f"Candidate 已存在：{candidate_id}")
        self._write_candidate(
            staging,
            source=target,
            parent_id=parent_id,
            changelog_source=resolved_workspace / "changelog.md",
        )
        os.replace(staging, directory)
        shutil.rmtree(resolved_workspace)
        return self.load_candidate(candidate_id)

    def load_candidate(self, candidate_id: str) -> CandidateRecord:
        """打开正式 Candidate 并验证 Target 内容摘要。"""
        directory = self._candidate_directory(candidate_id)
        if not directory.is_dir():
            raise ValueError(f"Candidate 不存在：{candidate_id}")
        target = directory / "target"
        expected = (directory / "target.sha256").read_text(encoding="utf-8").strip()
        actual = tree_digest(target)
        if actual != expected:
            raise ValueError(f"Candidate Target 摘要不匹配：{candidate_id}")
        parent_text = (directory / "parent").read_text(encoding="utf-8").strip()
        changelog_path = directory / "changelog.md"
        try:
            changelog_path.lstat()
        except FileNotFoundError:
            with changelog_path.open("x", encoding="utf-8"):
                pass
        read_changelog(changelog_path)
        return CandidateRecord(
            id=candidate_id,
            parent_id=parent_text or None,
            directory=directory,
            target_dir=target,
            changelog_path=changelog_path,
        )

    def load_rollback(self, candidate_id: str) -> RollbackRecord | None:
        """读取并校验恢复来源，只记录创建事实，不查询来源的当前评分。"""
        candidate = self.load_candidate(candidate_id)
        try:
            content = (candidate.directory / "rollback.json").read_bytes()
        except FileNotFoundError:
            return None
        record = RollbackRecord.model_validate_json(content)
        expected_log = (
            candidate.parent_id
            if record.changelog_source == "latest"
            else record.source_candidate_id
        )
        if (
            candidate.parent_id is None
            or record.changelog_source_candidate_id != expected_log
        ):
            raise ValueError("恢复日志来源与候选 parent/source 不一致")
        if record.source_candidate_id >= candidate.id:
            raise ValueError("恢复来源必须早于恢复候选")
        if tree_digest(candidate.target_dir) != record.source_target_sha256:
            raise ValueError("恢复 Target 与来源摘要不一致")
        return record

    def validate_rollback_results(self, candidate_id: str) -> None:
        """恢复产物缺失或归属错误时失败，不执行或重新复制评测。"""
        if self.load_rollback(candidate_id) is None:
            raise ValueError("缺少恢复记录")
        candidate = self.load_candidate(candidate_id)
        for role in (DatasetRole.TRAIN, DatasetRole.VALIDATION):
            snapshot = self._result_snapshot(candidate, role)
            if (
                role is DatasetRole.TRAIN
                and snapshot.evaluation.parent_comparison is None
            ):
                raise ValueError("恢复 Train 缺少父子比较")

    def publish_rollback(
        self,
        parent_id: str,
        source_id: str,
        *,
        candidate_id: str,
        changelog_source: RollbackChangelogSource,
        reason: RollbackReason,
        exploration_patience: int,
        exploration_depth: int | None,
    ) -> CandidateRecord:
        """一次性发布来自 source 的代码和评测，直接父级仍为 parent。"""
        parent, source = self.load_candidate(parent_id), self.load_candidate(source_id)
        self._require_rollback_target(parent, source)
        snapshots = {
            role: self._result_snapshot(source, role)
            for role in (DatasetRole.TRAIN, DatasetRole.VALIDATION)
        }
        parent_train = self._result_snapshot(parent, DatasetRole.TRAIN).evaluation
        record = RollbackRecord(
            source_candidate_id=source_id,
            changelog_source=changelog_source,
            changelog_source_candidate_id=parent_id
            if changelog_source == "latest"
            else source_id,
            reason=reason,
            exploration_patience=exploration_patience,
            exploration_depth=exploration_depth,
            source_target_sha256=tree_digest(source.target_dir),
            reused_artifacts={
                role: RollbackArtifactHashes(
                    execution_sha256=hashlib.sha256(s.execution_bytes).hexdigest(),
                    evaluation_sha256=hashlib.sha256(s.evaluation_bytes).hexdigest(),
                )
                for role, s in snapshots.items()
            },
        )
        directory = self._candidate_directory(candidate_id)
        staging = self._candidates / f".{candidate_id}.staging"
        if candidate_id <= max(parent_id, source_id):
            raise ValueError("恢复候选必须晚于父级与来源")
        if os.path.lexists(directory) or os.path.lexists(staging):
            raise FileExistsError(f"Candidate 已存在：{candidate_id}")
        try:
            self._stage_rollback(
                staging, candidate_id, parent, source, record, snapshots, parent_train
            )
            for role, snapshot in snapshots.items():
                role_dir = source.directory / role.value
                if (
                    role_dir / "execution.json"
                ).read_bytes() != snapshot.execution_bytes or (
                    role_dir / "evaluation.json"
                ).read_bytes() != snapshot.evaluation_bytes:
                    raise ValueError("恢复期间来源评测发生变化")
            if tree_digest(source.target_dir) != record.source_target_sha256:
                raise ValueError("恢复期间来源 Target 发生变化")
            os.replace(staging, directory)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return self.load_candidate(candidate_id)

    def _stage_rollback(
        self,
        staging: Path,
        candidate_id: str,
        parent: CandidateRecord,
        source: CandidateRecord,
        record: RollbackRecord,
        snapshots: dict[DatasetRole, _ResultSnapshot],
        parent_train: EvaluationResult,
    ) -> None:
        """在暂存目录重建回退候选、评分及日志，并核对代码摘要。"""
        self._write_candidate(
            staging,
            source=source.target_dir,
            parent_id=parent.id,
            changelog_source=(
                parent if record.changelog_source == "latest" else source
            ).changelog_path,
        )
        restored = CandidateRecord(
            id=candidate_id,
            parent_id=parent.id,
            directory=staging,
            target_dir=staging / "target",
            changelog_path=staging / "changelog.md",
        )
        for role, snapshot in snapshots.items():
            evaluation = _restored_evaluation(
                snapshot.evaluation, role, parent, parent_train, candidate_id
            )
            role_dir = staging / role.value
            atomic_write_json(
                role_dir / "execution.json", snapshot.execution.model_dump(mode="json")
            )
            atomic_write_text(
                role_dir / "evaluation.json",
                format_evaluation_json(evaluation.model_dump(mode="json")) + "\n",
            )
            self._result_snapshot(restored, role)
        atomic_write_json(staging / "rollback.json", record.model_dump(mode="json"))
        parent_validation = self.load_evaluation(parent.id, DatasetRole.VALIDATION)
        log = format_rollback(
            parent.id,
            candidate_id,
            record,
            parent_train,
            snapshots[DatasetRole.TRAIN].evaluation,
            snapshots[DatasetRole.VALIDATION].evaluation,
            parent_validation,
        )
        atomic_write_text(
            restored.changelog_path, log + read_changelog(restored.changelog_path)
        )
        if tree_digest(restored.target_dir) != record.source_target_sha256:
            raise ValueError("恢复 Target 与来源摘要不一致")

    def _result_snapshot(
        self, candidate: CandidateRecord, role: DatasetRole
    ) -> _ResultSnapshot:
        """读取阶段结果，校验 Case 集合和父子比较归属。"""
        role_dir = candidate.directory / role.value
        execution_bytes = (role_dir / "execution.json").read_bytes()
        evaluation_bytes = (role_dir / "evaluation.json").read_bytes()
        execution = ExecutionResult.model_validate_json(execution_bytes)
        evaluation = EvaluationResult.model_validate_json(evaluation_bytes)
        self._require_matching_ids(execution, evaluation)
        self._require_comparison_owner(candidate, role, evaluation)
        return _ResultSnapshot(execution_bytes, evaluation_bytes, execution, evaluation)

    def _require_rollback_target(
        self, parent: CandidateRecord, source: CandidateRecord
    ) -> None:
        """确认回退确实改变代码，且不会改变受保护的内容。"""
        if parent.id == source.id or tree_digest(parent.target_dir) == tree_digest(
            source.target_dir
        ):
            raise ValueError("相同 Target 不需要恢复")
        policy = WorkspaceAccess(
            read_only=self._read_only, inaccessible=self._inaccessible
        )
        directories = [
            {
                p.relative_to(c.target_dir).as_posix()
                for p in c.target_dir.rglob("*")
                if p.is_dir()
            }
            for c in (parent, source)
        ]
        changed_paths = {
            c.path for c in collect_changes(parent.target_dir, source.target_dir)
        }
        if any(
            policy.is_restricted(path)
            for path in changed_paths | (directories[0] ^ directories[1])
        ):
            raise ValueError("恢复变化影响工作区权限边界")

    def list_candidates(self) -> tuple[CandidateRecord, ...]:
        """按 Candidate ID 返回全部正式 Candidate。"""
        return tuple(
            self.load_candidate(path.name)
            for path in list_candidate_directories(self._experiment_root)
        )

    def save_execution(
        self,
        candidate_id: str,
        role: DatasetRole,
        execution: ExecutionResult,
    ) -> None:
        """原子发布 Execution，并使同 Role 的旧 Evaluation 失效。"""
        role_dir = self.load_candidate(candidate_id).directory / role.value
        role_dir.mkdir(exist_ok=True)
        (role_dir / "evaluation.json").unlink(missing_ok=True)
        atomic_write_json(
            role_dir / "execution.json",
            execution.model_dump(mode="json"),
        )

    def save_evaluation(
        self,
        candidate_id: str,
        role: DatasetRole,
        evaluation: EvaluationResult,
    ) -> None:
        """在 Execution Case 集合一致时发布 Evaluation。"""
        execution = self.load_execution(candidate_id, role)
        if execution is None:
            raise ValueError("发布 Evaluation 前必须存在 Execution")
        self._require_matching_ids(execution, evaluation)
        role_dir = self.load_candidate(candidate_id).directory / role.value
        atomic_write_text(
            role_dir / "evaluation.json",
            format_evaluation_json(evaluation.model_dump(mode="json")) + "\n",
        )

    def backfill_changelog(
        self, candidate_id: str, *, comparison: str, conclusion: str
    ) -> None:
        """覆盖当前 Candidate 的平台结果章节，保留历史和 Agent 分析。"""
        path = self.load_candidate(candidate_id).changelog_path
        original = read_changelog(path)
        content = _backfill_changelog_sections(
            original, candidate_id, comparison=comparison, conclusion=conclusion
        )
        if content is None:
            _LOGGER.warning("Changelog 缺少迭代版本，跳过回填：%s", candidate_id)
            return
        if content != original:
            atomic_write_text(path, content)

    def replace_test_result(
        self,
        candidate_id: str,
        execution: ExecutionResult,
        evaluation: EvaluationResult,
    ) -> None:
        """仅在两个 Test 文件都完成后整体替换原结果。"""
        self._require_matching_ids(execution, evaluation)
        candidate_dir = self.load_candidate(candidate_id).directory
        staging = candidate_dir / f".test-staging-{uuid4().hex}"
        backup = candidate_dir / f".test-backup-{uuid4().hex}"
        role_dir = candidate_dir / DatasetRole.TEST.value
        staging.mkdir()
        try:
            atomic_write_json(
                staging / "execution.json",
                execution.model_dump(mode="json"),
            )
            atomic_write_text(
                staging / "evaluation.json",
                format_evaluation_json(evaluation.model_dump(mode="json")) + "\n",
            )
            if role_dir.exists():
                os.replace(role_dir, backup)
            try:
                os.replace(staging, role_dir)
            except BaseException:
                if backup.exists():
                    os.replace(backup, role_dir)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def load_problem_library(self) -> ProblemLibrary:
        """读取实验级问题库；文件缺失时按空库处理。"""
        path = self._root / "problem_library.json"
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ProblemLibrary()
        return ProblemLibrary.model_validate_json(content)

    def save_problem_library(self, library: ProblemLibrary) -> None:
        """原子发布问题库快照，覆盖上一轮内容。"""
        atomic_write_json(
            self._root / "problem_library.json",
            library.model_dump(mode="json"),
        )

    def load_execution(
        self,
        candidate_id: str,
        role: DatasetRole,
    ) -> ExecutionResult | None:
        """读取已发布 Execution；不存在时返回 None。"""
        path = (
            self.load_candidate(candidate_id).directory / role.value / "execution.json"
        )
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return ExecutionResult.model_validate_json(content)

    def load_evaluation(
        self,
        candidate_id: str,
        role: DatasetRole,
    ) -> EvaluationResult | None:
        """读取已发布 Evaluation；不存在时返回 None。"""
        candidate = self.load_candidate(candidate_id)
        path = candidate.directory / role.value / "evaluation.json"
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        evaluation = EvaluationResult.model_validate_json(content)
        execution = self.load_execution(candidate_id, role)
        if execution is None:
            raise ValueError(
                f"Evaluation 缺少对应 Execution：{candidate_id}/{role.value}"
            )
        self._require_matching_ids(execution, evaluation)
        self._require_comparison_owner(candidate, role, evaluation)
        return evaluation

    @staticmethod
    def _require_comparison_owner(
        candidate: CandidateRecord, role: DatasetRole, evaluation: EvaluationResult
    ) -> None:
        """确保父子比较只属于当前候选的 Train 结果。"""
        comparison = evaluation.parent_comparison
        if comparison is not None and (
            role is not DatasetRole.TRAIN
            or comparison.current_candidate_id != candidate.id
            or comparison.previous_candidate_id != candidate.parent_id
        ):
            raise ValueError(
                f"Evaluation 挑战版本归属不一致：{candidate.id}/{role.value}"
            )

    def _write_candidate(
        self,
        staging: Path,
        *,
        source: Path,
        parent_id: str | None,
        changelog_source: Path | None,
    ) -> None:
        """创建候选暂存快照和摘要；失败时清理尚未发布的目录。"""
        if staging.exists():
            raise ValueError(f"Candidate staging 已存在：{staging.name}")
        staging.mkdir(parents=True)
        try:
            target = staging / "target"
            copy_tree(source, target)
            digest = tree_digest(target)
            (staging / "parent").write_text(parent_id or "", encoding="utf-8")
            (staging / "changelog.md").write_text(
                read_changelog(changelog_source)
                if changelog_source is not None
                else "",
                encoding="utf-8",
            )
            (staging / "target.sha256").write_text(digest + "\n", encoding="utf-8")
        except BaseException:
            shutil.rmtree(staging)
            raise

    def allocate_candidate_id(self) -> str:
        """预分配当前仓库实例内不复用的 ID，不创建产物目录。"""
        now = self._clock().astimezone().replace(microsecond=0)
        # 重启后也必须晚于已发布版本，不能填补失败尝试留下的旧 ID 空档。
        if self._last_candidate_time is None and self._candidates.exists():
            latest_id = max(
                (
                    path.name
                    for path in self._candidates.iterdir()
                    if path.is_dir() and _CANDIDATE_PATTERN.fullmatch(path.name)
                ),
                default="",
            )
            if latest_id:
                self._last_candidate_time = datetime.strptime(
                    latest_id, "candidate-%Y%m%dT%H%M%S"
                ).astimezone()
        if self._last_candidate_time is not None:
            now = max(now, self._last_candidate_time + timedelta(seconds=1))
        while True:
            candidate_id = now.strftime("candidate-%Y%m%dT%H%M%S")
            staging = self._candidates / f".{candidate_id}.staging"
            if not os.path.lexists(
                self._candidates / candidate_id
            ) and not os.path.lexists(staging):
                self._last_candidate_time = now
                return candidate_id
            now += timedelta(seconds=1)

    def _candidate_directory(self, candidate_id: str) -> Path:
        """校验 Candidate ID 格式后定位其正式目录。"""
        if not _CANDIDATE_PATTERN.fullmatch(candidate_id):
            raise ValueError(f"Candidate ID 非法：{candidate_id}")
        return self._candidates / candidate_id

    def _load_manifest(self) -> ExperimentManifest:
        """读取实验清单，并校验当前目标和保护策略与其一致。"""
        manifest = ExperimentManifest.model_validate_json(
            self._manifest_path.read_text(encoding="utf-8")
        )
        if (
            manifest.objective != self._objective
            or manifest.read_only != self._read_only
            or manifest.inaccessible != self._inaccessible
        ):
            raise ValueError("Experiment Manifest 与当前工作区配置不一致")
        return manifest

    @staticmethod
    def _require_matching_ids(
        execution: ExecutionResult,
        evaluation: EvaluationResult,
    ) -> None:
        """拒绝执行结果与评分结果包含不同 Case 集合的情况。"""
        execution_ids = {case.id for case in execution.cases}
        evaluation_ids = {case.id for case in evaluation.cases}
        if execution_ids != evaluation_ids:
            raise ValueError("Execution 与 Evaluation 的 Case ID 集合不一致")


def _restored_evaluation(
    source: EvaluationResult,
    role: DatasetRole,
    parent: CandidateRecord,
    parent_train: EvaluationResult,
    candidate_id: str,
) -> EvaluationResult:
    """复用来源分数，并按恢复节点的真实父级重建比较信息。"""
    previous = {case.id: case.score for case in parent_train.cases}
    if role is DatasetRole.TRAIN and previous.keys() != {
        case.id for case in source.cases
    }:
        raise ValueError("恢复来源与父级 Train Case 集合不一致")
    result = source.model_copy(
        update={
            "parent_comparison": None,
            "cases": tuple(
                case.model_copy(
                    update={
                        "previous_score": previous[case.id]
                        if role is DatasetRole.TRAIN
                        else None,
                    }
                )
                for case in source.cases
            ),
        }
    )
    if role is DatasetRole.TRAIN:
        result = result.with_comparison(
            compare_evaluations(parent.id, parent_train, candidate_id, result)
        )
    return result


def list_candidate_directories(experiment_root: Path) -> tuple[Path, ...]:
    """只读列举正式候选目录，不初始化实验或加载、修复候选内容。"""
    root = experiment_root / ".evolution/candidates"
    if not root.exists():
        return ()
    return tuple(
        sorted(
            path
            for path in root.iterdir()
            if path.is_dir() and _CANDIDATE_PATTERN.fullmatch(path.name)
        )
    )


def tree_digest(root: Path) -> str:
    """计算拒绝符号链接且与遍历顺序无关的目录摘要。"""
    digest = hashlib.sha256()
    for relative, path in _regular_files(root).items():
        encoded = relative.encode()
        content = path.read_bytes()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def copy_tree(source: Path, destination: Path) -> None:
    """复制无符号链接目录，保留源文件属性，不额外调整权限。"""
    _regular_files(source)
    shutil.copytree(source, destination)


def collect_changes(before: Path, after: Path) -> tuple[FileChange, ...]:
    """返回两个目录之间按路径排序的最终净变化。"""
    before_files = _regular_files(before)
    after_files = _regular_files(after)
    changes: list[FileChange] = []
    for relative in sorted(before_files.keys() | after_files.keys()):
        old_path = before_files.get(relative)
        new_path = after_files.get(relative)
        old = old_path.read_bytes() if old_path is not None else None
        new = new_path.read_bytes() if new_path is not None else None
        if old == new:
            continue
        kind: Literal["added", "modified", "deleted"]
        if old is None:
            kind = "added"
        elif new is None:
            kind = "deleted"
        else:
            kind = "modified"
        changes.append(_file_change(relative, kind, old, new))
    return tuple(changes)


def atomic_write_json(path: Path, value: JsonValue) -> None:
    """在目标同目录完整落盘后原子替换 JSON 文件。"""
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_write_text(
    path: Path,
    content: str,
) -> None:
    """在目标同目录完整落盘后原子替换 UTF-8 文本文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _regular_files(root: Path) -> dict[str, Path]:
    """稳定排序枚举普通文件，拒绝目录树中的链接和特殊文件。"""
    if root.is_symlink():
        raise ValueError(f"目录不得是符号链接：{root}")
    if not root.is_dir():
        raise ValueError(f"目录不存在：{root}")
    files: dict[str, Path] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise ValueError(f"Target/Workspace 不允许符号链接：{directory}")
        for name in filenames:
            path = current_path / name
            if path.is_symlink():
                raise ValueError(f"Target/Workspace 不允许符号链接：{path}")
            if not path.is_file():
                raise ValueError(f"Target/Workspace 只允许普通文件：{path}")
            files[path.relative_to(root).as_posix()] = path
    return dict(sorted(files.items()))


def _file_change(
    relative: str,
    kind: Literal["added", "modified", "deleted"],
    before: bytes | None,
    after: bytes | None,
) -> FileChange:
    """生成单文件变更记录，保留前后字节数、摘要及可用文本差异。"""
    text_diff = _text_diff(relative, before, after)
    return FileChange(
        path=relative,
        kind=kind,
        diff=text_diff,
        before_sha256=_sha256(before),
        after_sha256=_sha256(after),
        before_bytes=len(before) if before is not None else None,
        after_bytes=len(after) if after is not None else None,
    )


def _text_diff(relative: str, before: bytes | None, after: bytes | None) -> str | None:
    """生成 UTF-8 unified diff；二进制或无法解码时返回 None。"""
    if _looks_binary(before) or _looks_binary(after):
        return None
    try:
        old_lines = _decode_diff_lines(before)
        new_lines = _decode_diff_lines(after)
    except UnicodeDecodeError:
        return None
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )


def _decode_diff_lines(content: bytes | None) -> list[str]:
    """保留换行拆分 UTF-8 内容，并标记缺失的末尾换行。"""
    if content is None:
        return []
    lines = content.decode("utf-8").splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines


def _looks_binary(content: bytes | None) -> bool:
    """按空字节和控制字符比例判断内容是否应省略文本差异。"""
    if not content:
        return False
    if b"\x00" in content:
        return True
    control_bytes = sum(byte < 32 and byte not in b"\t\n\r" for byte in content)
    return control_bytes * 10 > len(content)


def _sha256(content: bytes | None) -> str | None:
    """计算内容摘要；None 表示该侧文件不存在。"""
    return hashlib.sha256(content).hexdigest() if content is not None else None


def read_changelog(path: Path) -> str:
    """读取普通 UTF-8 日志，拒绝链接、目录和损坏文本。"""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Changelog 必须是普通文件：{path}")
    return path.read_text(encoding="utf-8")


def format_score_change(previous: float, current: float) -> str:
    """以固定精度展示前后分数与增量。"""
    delta = f"{current - previous:+.4f}".replace("-", "−")
    return f"{previous:.4f} → {current:.4f}（{delta}）"


def format_train_comparison(train: EvaluationResult) -> str:
    """展示已保存的父子比较，保留 Case 集合不可比的语义。"""
    comparison = train.parent_comparison
    if comparison is None:
        raise ValueError("非 Seed Train 缺少父子比较")
    change = format_score_change(comparison.previous_total_score, train.total_score)
    regressed = tuple(
        case.id
        for case in train.cases
        if case.previous_score is not None and case.score < case.previous_score
    )
    case_summary = (
        "逐题集合不可比。"
        if comparison.improved_case_count is None
        else (
            f"改善 {comparison.improved_case_count} 个 case，"
            f"退步 {comparison.regressed_case_count} 个，"
            f"不变 {comparison.unchanged_case_count} 个。\n"
            f"退步 case：{'、'.join(regressed) if regressed else '无'}。"
        )
    )
    return f"训练集：{change}。\n{case_summary}"


def format_exploration_route(
    *,
    rollback_enabled: bool,
    depth: int | None,
    exploration_patience: int,
) -> str:
    """展示 Engine 已计算的探索状态，不执行回退判断或文件操作。"""
    if not rollback_enabled:
        return "自动回退已关闭；若有下一轮优化预算，继续从本候选探索。"
    if depth is None:
        return "当前链不包含验证最佳代码，若有下一轮优化预算，将先恢复至验证最佳。"
    route = f"连续未刷新 Validation 最佳的探索深度：{depth}/{exploration_patience}。"
    return route + (
        "若有下一轮优化预算，将先恢复至验证最佳。"
        if depth >= exploration_patience
        else "若有下一轮优化预算，继续从本候选探索。"
    )


def _backfill_changelog_sections(
    content: str,
    candidate_id: str,
    *,
    comparison: str,
    conclusion: str,
) -> str | None:
    """回填唯一版本的评测章节；缺少版本时返回 None，重复标题时拒绝。"""
    headings = _changelog_headings(content)
    rounds = [
        index
        for index, (_, _, title) in enumerate(headings)
        if title == f"## 迭代版本：{candidate_id}"
    ]
    if not rounds:
        return None
    if len(rounds) != 1:
        raise ValueError(f"Changelog 迭代版本缺失或重复：{candidate_id}")
    sections = headings[rounds[0] + 1 :]
    boundary = next(
        (
            index
            for index, (_, _, title) in enumerate(sections)
            if title.startswith("## ")
        ),
        len(sections),
    )
    end = sections[boundary][0] if boundary < len(sections) else len(content)
    return _replace_changelog_sections(
        content,
        sections[:boundary],
        end,
        comparison=comparison,
        conclusion=conclusion,
    )


def format_rollback(
    parent_id: str,
    candidate_id: str,
    record: RollbackRecord,
    parent_train: EvaluationResult,
    source_train: EvaluationResult,
    source_validation: EvaluationResult,
    parent_validation: EvaluationResult | None,
) -> str:
    """生成平台恢复记录，不把复用的评测描述成重新执行。"""
    if record.reason == "anchor_not_in_lineage":
        reason = (
            "当前候选链不包含验证最佳 V 的等价代码版本，恢复至\n"
            f"{record.source_candidate_id}。"
            f"最新候选 C 的 Train 为 {parent_train.total_score:.4f}，"
            f"V 的 Train 为 {source_train.total_score:.4f}。\n"
        )
    else:
        score = (
            f"{parent_validation.total_score:.4f}"
            if parent_validation is not None
            else "未执行"
        )
        reason = (
            f"连续 {record.exploration_depth} 个探索候选未提升验证最佳 V 的 Validation"
            f"（{source_validation.total_score:.4f}）。\n"
            f"最新候选 C 的 Validation 为 {score}，结束本次探索路线。\n"
        )
    text = (
        f"## 迭代版本：{candidate_id}\n\n"
        "### 操作类型\n平台自动回退。\n\n### 回退原因\n" + reason + "\n"
        "### 恢复与继承\n"
        f"- 代码恢复来源：{record.source_candidate_id}。\n"
        f"- 日志模式：{record.changelog_source}。\n"
        f"- 日志继承来源：{record.changelog_source_candidate_id}。\n"
        f"- R 的直接父版本：{parent_id}。\n"
        f"- Train 和 Validation 结果复用自 {record.source_candidate_id}。\n\n"
    )
    if record.changelog_source == "latest":
        text += (
            "### 后续复盘说明\n"
            "当前代码已恢复为 V，V 之后日志记录的修改已撤销，不存在于当前代码中。\n\n"
        )
    return text


def _replace_changelog_sections(
    content: str,
    sections: list[tuple[int, int, str]],
    end: int,
    *,
    comparison: str,
    conclusion: str,
) -> str:
    """替换已有平台正文或补齐标题，所有位置校验完成后再应用修改。"""
    edits: list[tuple[int, int, str]] = []
    additions: dict[int, list[str]] = {}
    for title, replacement in (
        ("### 评测对比", comparison),
        ("### 结论", conclusion),
    ):
        replacement = replacement.rstrip("\n")
        matches = [i for i, (_, _, heading) in enumerate(sections) if heading == title]
        if len(matches) > 1:
            raise ValueError(f"Changelog 章节重复：{title}")
        if not matches:
            position = end
            if title == "### 评测对比":
                position = next(
                    (start for start, _, heading in sections if heading == "### 结论"),
                    end,
                )
            additions.setdefault(position, []).append(f"{title}\n{replacement}\n")
            continue
        index = matches[0]
        start = sections[index][1]
        stop = sections[index + 1][0] if index + 1 < len(sections) else end
        body = content[start:stop]
        trailing = body[len(body.rstrip("\n")) :] or "\n"
        separator = "" if content[:start].endswith("\n") else "\n"
        edits.append((start, stop, separator + replacement + trailing))
    for position, blocks in additions.items():
        separator = "" if content[:position].endswith("\n") else "\n"
        edits.append((position, position, separator + "\n".join(blocks) + "\n"))
    # 同一位置先插入后续标题，再写空章节正文，避免按正文字符串排序而串节。
    for start, stop, replacement in reversed(sorted(edits, key=lambda edit: edit[:2])):
        content = content[:start] + replacement + content[stop:]
    return content


def _changelog_headings(content: str) -> list[tuple[int, int, str]]:
    """定位版本和章节标题，跳过文件变更中围栏代码块里的示例。"""
    headings: list[tuple[int, int, str]] = []
    fence = ""
    offset = 0
    for line in content.splitlines(keepends=True):
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if marker:
            token, suffix = marker.groups()
            if not fence:
                fence = token
            elif (
                token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip()
            ):
                fence = ""
        elif not fence and re.match(r"#{2,3} ", line):
            headings.append((offset, offset + len(line), line.rstrip()))
        offset += len(line)
    return headings


__all__ = [
    "CandidateRecord",
    "CandidateRepository",
    "FileChange",
    "atomic_write_json",
    "collect_changes",
    "copy_tree",
    "list_candidate_directories",
    "tree_digest",
]
