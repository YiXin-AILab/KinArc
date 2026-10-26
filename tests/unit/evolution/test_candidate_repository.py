"""Candidate 文件事实仓库测试。"""

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agentsystem.common.models import JsonValue
from evolution.candidates import CandidateRecord, CandidateRepository, collect_changes
from evolution.candidates import atomic_write_json as real_atomic_write_json
from evolution.diagnosis import CaseSeverity, ProblemLibrary, ProblemLibraryEntry
from evolution.models import (
    CaseEvaluation,
    CaseExecution,
    DatasetRole,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
    RollbackChangelogSource,
)


class _Clock:
    """为测试产生不同毫秒的确定性时钟。"""

    def __init__(self) -> None:
        self._current = datetime(2026, 9, 1).astimezone()

    def __call__(self) -> datetime:
        value = self._current
        self._current += timedelta(milliseconds=1)
        return value


def _rollback_chain(
    repository: CandidateRepository,
) -> tuple[CandidateRecord, CandidateRecord]:
    seed = repository.initialize_seed()
    seed.changelog_path.chmod(0o644)
    seed.changelog_path.write_text("V 日志\n")
    for role in (DatasetRole.TRAIN, DatasetRole.VALIDATION, DatasetRole.TEST):
        repository.save_execution(seed.id, role, _execution())
        repository.save_evaluation(seed.id, role, _evaluation(score=0.8))
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("C")
    (workspace / "changelog.md").write_text(
        "C 独有分析\nCHANGE_RESULT_C_pending\nV 日志\n"
    )
    current = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert current is not None
    repository.save_execution(current.id, DatasetRole.TRAIN, _execution())
    repository.save_evaluation(current.id, DatasetRole.TRAIN, _evaluation(score=0.9))
    return seed, current


@pytest.mark.parametrize("mode", ["latest", "best"])
def test_publish_rollback_copies_complete_evidence_and_log_source(
    repository: CandidateRepository,
    mode: RollbackChangelogSource,
) -> None:
    seed, current = _rollback_chain(repository)
    before = {
        p: p.read_bytes()
        for c in (seed, current)
        for p in c.directory.rglob("*")
        if p.is_file()
    }
    restored = repository.publish_rollback(
        current.id,
        seed.id,
        candidate_id=repository.allocate_candidate_id(),
        changelog_source=mode,
        reason="exploration_patience_exhausted",
        exploration_patience=2,
        exploration_depth=2,
    )
    assert restored.parent_id == current.id
    assert not collect_changes(seed.target_dir, restored.target_dir)
    for path in (restored.changelog_path, restored.directory / "rollback.json"):
        assert path.stat().st_mode & 0o200
    train = repository.load_evaluation(restored.id, DatasetRole.TRAIN)
    assert train is not None and train.parent_comparison is not None
    assert train.total_score == 0.8 and train.cases[0].previous_score == 0.9
    assert train.parent_comparison.previous_candidate_id == current.id
    assert train.parent_comparison.current_candidate_id == restored.id
    assert repository.load_execution(restored.id, DatasetRole.TRAIN) == _execution()
    validation = repository.load_evaluation(restored.id, DatasetRole.VALIDATION)
    assert validation == _evaluation(score=0.8)
    assert not (restored.directory / "test").exists()
    log = restored.changelog_path.read_text()
    assert log.startswith(f"## 迭代版本：{restored.id}\n")
    restoration = log.split("### 恢复与继承", 1)[0]
    assert "## 评测版本：" not in restoration
    assert "rollback_enabled" not in restoration
    assert "exploration_patience" not in restoration
    assert f"Train 和 Validation 结果复用自 {seed.id}。" in log
    assert "未重新执行或评分" not in log and "未执行" in log
    assert ("C 独有分析" in log) == (mode == "latest")
    assert ("CHANGE_RESULT_C_pending" in log) == (mode == "latest")
    assert ("V 之后日志记录的修改已撤销" in log) == (mode == "latest")
    record = repository.load_rollback(restored.id)
    assert record is not None and record.source_candidate_id == seed.id
    assert record.changelog_source_candidate_id == (
        current.id if mode == "latest" else seed.id
    )
    repository.validate_rollback_results(restored.id)
    assert all(p.read_bytes() == value for p, value in before.items())
    repository.save_evaluation(seed.id, DatasetRole.TRAIN, _evaluation(score=0.1))
    repository.validate_rollback_results(restored.id)
    assert repository.load_evaluation(restored.id, DatasetRole.TRAIN) == train


@pytest.mark.parametrize("stage", ["train", "validation", "rollback", "rename"])
def test_rollback_publication_failure_leaves_no_partial_candidate(
    repository: CandidateRepository,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    seed, current = _rollback_chain(repository)
    candidate_id = repository.allocate_candidate_id()
    before = {
        p: p.read_bytes()
        for c in (seed, current)
        for p in c.directory.rglob("*")
        if p.is_file()
    }
    real_replace = os.replace

    def write(path: Path, value: JsonValue) -> None:
        if (stage in ("train", "validation") and path.parent.name == stage) or (
            stage == "rollback" and path.name == "rollback.json"
        ):
            raise OSError("injected write failure")
        real_atomic_write_json(path, value)

    def replace(source: Path, destination: Path) -> None:
        if stage == "rename" and Path(destination).name == candidate_id:
            raise OSError("injected rename failure")
        real_replace(source, destination)

    monkeypatch.setattr("evolution.candidates.atomic_write_json", write)
    monkeypatch.setattr("evolution.candidates.os.replace", replace)
    with pytest.raises(OSError, match="injected"):
        repository.publish_rollback(
            current.id,
            seed.id,
            candidate_id=candidate_id,
            changelog_source="best",
            reason="exploration_patience_exhausted",
            exploration_patience=1,
            exploration_depth=1,
        )
    assert repository.list_candidates() == (seed, current)
    assert not (seed.directory.parent / f".{candidate_id}.staging").exists()
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize(
    "file",
    [
        "train/execution.json",
        "train/evaluation.json",
        "validation/execution.json",
        "validation/evaluation.json",
    ],
)
def test_rollback_rejects_incomplete_source_and_restored_results(
    repository: CandidateRepository,
    file: str,
) -> None:
    seed, current = _rollback_chain(repository)
    restored = repository.publish_rollback(
        current.id,
        seed.id,
        candidate_id=repository.allocate_candidate_id(),
        changelog_source="best",
        reason="exploration_patience_exhausted",
        exploration_patience=1,
        exploration_depth=1,
    )
    (restored.directory / file).unlink()
    with pytest.raises(FileNotFoundError):
        repository.validate_rollback_results(restored.id)
    (seed.directory / file).unlink()
    with pytest.raises(FileNotFoundError):
        repository.publish_rollback(
            current.id,
            seed.id,
            candidate_id=repository.allocate_candidate_id(),
            changelog_source="best",
            reason="exploration_patience_exhausted",
            exploration_patience=1,
            exploration_depth=1,
        )
    assert len(repository.list_candidates()) == 3


@pytest.mark.parametrize(
    "damage", ["json", "log_source", "target_hash", "comparison", "cases"]
)
def test_rollback_rejects_malformed_records_and_result_ownership(
    repository: CandidateRepository, damage: str
) -> None:
    seed, current = _rollback_chain(repository)
    restored = repository.publish_rollback(
        current.id,
        seed.id,
        candidate_id=repository.allocate_candidate_id(),
        changelog_source="best",
        reason="exploration_patience_exhausted",
        exploration_patience=1,
        exploration_depth=1,
    )
    path = restored.directory / "rollback.json"
    if damage in {"comparison", "cases"}:
        path = restored.directory / "train/evaluation.json"
    data = json.loads(path.read_text())
    if damage == "log_source":
        data["changelog_source_candidate_id"] = current.id
    elif damage == "target_hash":
        data["source_target_sha256"] = "0" * 64
    elif damage == "comparison":
        data["parent_comparison"]["current_candidate_id"] = seed.id
    elif damage == "cases":
        data["cases"][0]["id"] = "different"
    path.chmod(0o644)
    path.write_text("{" if damage == "json" else json.dumps(data))
    with pytest.raises(ValueError):
        repository.validate_rollback_results(restored.id)


def test_rollback_requires_comparable_parent_train(
    repository: CandidateRepository,
) -> None:
    seed, current = _rollback_chain(repository)
    repository.save_execution(
        current.id, DatasetRole.TRAIN, _execution(case_id="different")
    )
    repository.save_evaluation(
        current.id, DatasetRole.TRAIN, _evaluation(case_id="different")
    )
    with pytest.raises(ValueError, match="Case"):
        repository.publish_rollback(
            current.id,
            seed.id,
            candidate_id=repository.allocate_candidate_id(),
            changelog_source="best",
            reason="exploration_patience_exhausted",
            exploration_patience=1,
            exploration_depth=1,
        )
    assert repository.list_candidates() == (seed, current)


def test_rollback_rejects_changing_source(
    repository: CandidateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, current = _rollback_chain(repository)

    def write(path: Path, value: JsonValue) -> None:
        real_atomic_write_json(path, value)
        if path.name == "rollback.json":
            repository.save_evaluation(
                seed.id, DatasetRole.TRAIN, _evaluation(score=0.1)
            )

    monkeypatch.setattr("evolution.candidates.atomic_write_json", write)
    with pytest.raises(ValueError, match="来源评测发生变化"):
        repository.publish_rollback(
            current.id,
            seed.id,
            candidate_id=repository.allocate_candidate_id(),
            changelog_source="best",
            reason="exploration_patience_exhausted",
            exploration_patience=1,
            exploration_depth=1,
        )
    assert repository.list_candidates() == (seed, current)


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[CandidateRepository]:
    """创建含可变 Prompt 和冻结数据的最小仓库。"""
    target = tmp_path / "initial"
    (target / "prompt").mkdir(parents=True)
    (target / "data").mkdir()
    (target / "prompt" / "system.md").write_text("original\n", encoding="utf-8")
    (target / "data" / "db.json").write_text("{}\n", encoding="utf-8")
    repo = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="improve",
        read_only=("data/", "reserved/"),
        clock=_Clock(),
    )
    yield repo


def test_candidate_and_workspace_preserve_source_modes(
    repository: CandidateRepository,
) -> None:
    """发布和创建工作区不增删文件权限，元数据保持正常可写。"""
    source = repository.initial_target
    source.chmod(0o750)
    (source / "prompt/system.md").chmod(0o640)
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    for target in (seed.target_dir, workspace / "target"):
        assert target.stat().st_mode & 0o777 == 0o750
        assert (target / "prompt/system.md").stat().st_mode & 0o777 == 0o640
    for name in ("parent", "changelog.md", "target.sha256"):
        assert (seed.directory / name).stat().st_mode & 0o200
    seed.changelog_path.write_text(
        f"## 迭代版本：{seed.id}\n### 评测对比\npending\n\n### 结论\npending\n"
    )
    repository.backfill_changelog(seed.id, comparison="comparison", conclusion="done")
    assert seed.changelog_path.read_text() == (
        f"## 迭代版本：{seed.id}\n### 评测对比\ncomparison\n\n### 结论\ndone\n"
    )
    assert seed.changelog_path.stat().st_mode & 0o200


def test_workspaces_use_project_temporary_root_and_isolate_experiments(
    tmp_path: Path,
) -> None:
    """临时副本集中到项目目录，实验间不能互相发布或清理。"""
    target = tmp_path / "target"
    target.mkdir()
    temporary_root = tmp_path / ".evolution"
    repositories = [
        CandidateRepository(
            tmp_path / name / "experiment",
            workspace_root=temporary_root,
            initial_target=target,
            objective="test",
            read_only=(),
        )
        for name in ("first", "second")
    ]
    seeds = [repo.initialize_seed() for repo in repositories]
    workspaces = [
        repo.create_workspace(seed.id, changelog_source_id=seed.id)
        for repo, seed in zip(repositories, seeds, strict=True)
    ]
    assert all(path.parent == temporary_root for path in workspaces)
    assert workspaces[0].name.split("-")[1] != workspaces[1].name.split("-")[1]
    for seed in seeds:
        assert not (seed.directory.parent.parent / "workspaces").exists()
    with pytest.raises(ValueError, match="Workspace"):
        repositories[0].discard_workspace(workspaces[1])
    with pytest.raises(ValueError, match="Workspace"):
        repositories[0].publish_workspace(
            seeds[0].id,
            workspaces[1],
            candidate_id=repositories[0].allocate_candidate_id(),
        )
    repositories[0].discard_workspace(workspaces[0])
    assert workspaces[1].is_dir()


@pytest.mark.parametrize("filled", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_changelog_backfill_replaces_own_sections_repeatedly(
    repository: CandidateRepository, filled: bool, reverse: bool
) -> None:
    """占位和已填正文均可覆盖，继承记录、代码示例与后续版本不变。"""
    seed = repository.initialize_seed()
    candidate_id = repository.allocate_candidate_id()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("changed")
    inherited = f"## 迭代版本：{seed.id}\n### 评测对比\n历史比较\n### 结论\n历史结论\n"
    analysis = (
        f"## 评测版本：{seed.id}\n## 迭代版本：{candidate_id}\n"
        "### 文件变更\n```markdown\n### 结论\n示例\n```\n"
        "### 相关CASE测试结果\nCHANGE_RESULT_test\n"
    )
    comparison = "旧比较" if filled else f"EVALUATION_COMPARISON_{candidate_id}"
    conclusion = "旧结论" if filled else f"ADOPTION_RESULT_{candidate_id}"
    following = "## 评测版本：later\n## 迭代版本：later\n### 结论\n后续结论\n"
    if reverse:
        inherited, following = following, inherited
    original = (
        inherited
        + analysis
        + f"### 评测对比\n{comparison}\n\n### 结论\n{conclusion}\n\n"
        + following
    )
    (workspace / "changelog.md").write_text(original)
    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=candidate_id
    )
    assert candidate is not None
    for comparison, conclusion in (("比较", "结论"), ("新比较", "新结论")):
        repository.backfill_changelog(
            candidate_id, comparison=comparison, conclusion=conclusion
        )
        expected = (
            inherited
            + analysis
            + f"### 评测对比\n{comparison}\n\n### 结论\n{conclusion}\n\n"
            + following
        )
        assert candidate.changelog_path.read_text() == expected
        modified = candidate.changelog_path.stat().st_mtime_ns
        repository.backfill_changelog(
            candidate_id, comparison=comparison, conclusion=conclusion
        )
        assert candidate.changelog_path.read_text() == expected
        assert candidate.changelog_path.stat().st_mtime_ns == modified
        assert candidate.changelog_path.stat().st_mode & 0o200


@pytest.mark.parametrize(
    "malformed",
    ["missing_round", "duplicate_round", "duplicate_section"],
)
def test_changelog_backfill_rejects_ambiguous_or_missing_sections(
    repository: CandidateRepository, malformed: str, caplog: pytest.LogCaptureFixture
) -> None:
    """无本轮记录时提示并跳过；重复标题时不部分回填。"""
    seed = repository.initialize_seed()
    candidate_id = repository.allocate_candidate_id()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("changed")
    title = f"## 迭代版本：{candidate_id}\n"
    original = title + "### 评测对比\n旧比较\n### 结论\n旧结论\n"
    if malformed == "missing_round":
        original = original.replace(title, "")
    elif malformed == "duplicate_round":
        original += title
    else:
        original += "### 结论\n重复结论\n"
    (workspace / "changelog.md").write_text(original)
    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=candidate_id
    )
    assert candidate is not None

    if malformed == "missing_round":
        repository.backfill_changelog(
            candidate_id, comparison="新比较", conclusion="新结论"
        )
        assert "跳过回填" in caplog.text
    else:
        with pytest.raises(ValueError, match="Changelog"):
            repository.backfill_changelog(
                candidate_id, comparison="新比较", conclusion="新结论"
            )
    assert candidate.changelog_path.read_text() == original


@pytest.mark.parametrize(
    "existing",
    [
        "",
        "### 评测对比\n旧比较\n",
        "### 结论\n旧结论\n",
        "### 评测对比\n",
        "### 结论",
        "### 评测对比",
    ],
)
@pytest.mark.parametrize("following", ["", "\n## 评测版本：later\n历史内容\n"])
def test_changelog_backfill_creates_missing_platform_sections(
    repository: CandidateRepository, existing: str, following: str
) -> None:
    """模型不写平台标题也能回填，只缺一个时补齐，并保持后续版本不变。"""
    seed = repository.initialize_seed()
    candidate_id = repository.allocate_candidate_id()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("changed")
    prefix = f"## 迭代版本：{candidate_id}\n### 相关CASE测试结果\n分析原文"
    original = prefix + ("\n" + existing if existing else "") + following
    (workspace / "changelog.md").write_text(original)
    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=candidate_id
    )
    assert candidate is not None

    repository.backfill_changelog(
        candidate_id, comparison="新比较", conclusion="新结论"
    )

    result = candidate.changelog_path.read_text()
    assert result.startswith(prefix)
    assert result.count("### 评测对比\n新比较\n") == 1
    assert result.count("### 结论\n新结论\n") == 1
    assert result.index("### 评测对比") < result.index("### 结论")
    assert "旧比较" not in result and "旧结论" not in result
    if following:
        assert result.endswith(following)
        assert result.index("### 结论") < result.index("## 评测版本：later")
    repository.backfill_changelog(
        candidate_id, comparison="新比较", conclusion="新结论"
    )
    assert candidate.changelog_path.read_text() == result


def test_allocated_ids_are_unique_before_publication(
    repository: CandidateRepository,
) -> None:
    """连续预分配不复用 ID，也不创建占位产物。"""
    seed = repository.initialize_seed()
    first = repository.allocate_candidate_id()
    second = repository.allocate_candidate_id()
    assert len({seed.id, first, second}) == 3
    assert not (seed.directory.parent / first).exists()
    assert not (seed.directory.parent / second).exists()


def test_reopened_repository_allocates_after_latest_published_id(
    tmp_path: Path,
) -> None:
    """重启时不能复用早于最新发布版本的失败尝试空档。"""
    target = tmp_path / "target"
    target.mkdir()
    (target / "a").write_text("seed")

    def clock() -> datetime:
        return datetime(2026, 9, 1, tzinfo=UTC)

    repo = CandidateRepository(
        tmp_path,
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="x",
        read_only=(),
        clock=clock,
    )
    seed = repo.initialize_seed()
    repo.allocate_candidate_id()
    workspace = repo.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/a").write_text("child")
    child = repo.publish_workspace(
        seed.id, workspace, candidate_id=repo.allocate_candidate_id()
    )
    assert child is not None
    reopened = CandidateRepository(
        tmp_path,
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="x",
        read_only=(),
        clock=clock,
    )
    assert reopened.allocate_candidate_id() > child.id


def test_publication_uses_allocated_id_and_rejects_collision(
    repository: CandidateRepository,
) -> None:
    """发布必须使用预分配 ID，同名产物不能被覆盖。"""
    seed = repository.initialize_seed()
    candidate_id = repository.allocate_candidate_id()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("first")
    child = repository.publish_workspace(seed.id, workspace, candidate_id=candidate_id)
    assert child is not None and child.id == candidate_id
    other = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (other / "target/prompt/system.md").write_text("second")
    with pytest.raises(FileExistsError):
        repository.publish_workspace(seed.id, other, candidate_id=candidate_id)
    assert (child.target_dir / "prompt/system.md").read_text() == "first"
    assert other.is_dir()


def test_problem_library_roundtrip_and_missing_file(
    repository: CandidateRepository,
) -> None:
    """问题库落盘后可原样读回并覆盖更新；旧实验缺文件时按空库处理。"""
    assert repository.load_problem_library() == ProblemLibrary()
    library = ProblemLibrary(
        entries=(
            ProblemLibraryEntry(
                signature="timeout",
                severity=CaseSeverity.TOTAL_FAILURE,
                case_ids=("case-a",),
                first_seen_candidate_id="candidate-20260901T000000",
                last_seen_candidate_id="candidate-20260901T000000",
                status="accumulating",
            ),
        )
    )
    repository.save_problem_library(library)
    assert repository.load_problem_library() == library
    matured = ProblemLibrary(
        entries=(
            library.entries[0].model_copy(
                update={"case_ids": ("case-a", "case-b"), "status": "ready"}
            ),
        )
    )
    repository.save_problem_library(matured)
    assert repository.load_problem_library() == matured
    assert repository.load_problem_library() is not matured


def _execution(case_id: str = "task_001") -> ExecutionResult:
    metadata = ExecutionMetadata(total_tokens=2, duration_seconds=0.1)
    return ExecutionResult(
        cases=(
            CaseExecution(
                id=case_id,
                result="succeeded",
                tracelog={},
                metadata=metadata,
            ),
        )
    )


def _evaluation(case_id: str = "task_001", score: float = 1.0) -> EvaluationResult:
    metadata = ExecutionMetadata(total_tokens=2, duration_seconds=0.1)
    return EvaluationResult(
        total_score=score,
        cases=(
            CaseEvaluation(
                id=case_id,
                result="succeeded",
                tracelog={},
                metadata=metadata,
                expected={},
                score=score,
                feedback="ok",
            ),
        ),
    )


def test_candidate_inherits_independent_changelog(
    repository: CandidateRepository,
) -> None:
    """子级拷贝历史；只改日志不发布，也不修改父级。"""
    seed = repository.initialize_seed()
    workspace_root = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    assert (workspace_root / "target").is_dir()
    log = workspace_root / "changelog.md"
    assert log.read_text(encoding="utf-8") == ""
    log.write_text("first round\n", encoding="utf-8")
    (workspace_root / "target/prompt/system.md").write_text("improved\n")
    child = repository.publish_workspace(
        seed.id, workspace_root, candidate_id=repository.allocate_candidate_id()
    )
    assert child is not None
    assert child.changelog_path.read_text() == "first round\n"
    assert seed.changelog_path.read_text() == ""
    assert not (child.directory / "change.log").exists()
    workspace_root = repository.create_workspace(child.id, changelog_source_id=child.id)
    assert (workspace_root / "changelog.md").read_text() == "first round\n"
    (workspace_root / "changelog.md").write_text("updated result\n")
    assert (
        repository.publish_workspace(
            child.id, workspace_root, candidate_id=repository.allocate_candidate_id()
        )
        is None
    )
    assert not workspace_root.exists()
    assert child.changelog_path.read_text() == "first round\n"


@pytest.mark.parametrize(
    "node", ["missing", "existing", "directory", "symlink", "invalid"]
)
def test_load_changelog_preserves_content_or_reports_errors(
    repository: CandidateRepository,
    node: str,
) -> None:
    """只有缺失才创建空文件，不掩盖异常节点或损坏文本。"""
    seed = repository.initialize_seed()
    log = seed.directory / "changelog.md"
    log.unlink(missing_ok=True)
    if node == "existing":
        log.write_text("history")
    elif node == "directory":
        log.mkdir()
    elif node == "symlink":
        log.symlink_to(log.parent / "missing")
    elif node == "invalid":
        log.write_bytes(b"\xff")
    if node in {"directory", "symlink", "invalid"}:
        with pytest.raises((ValueError, OSError)):
            repository.load_candidate(seed.id)
    else:
        loaded = repository.load_candidate(seed.id)
        assert loaded.changelog_path.read_text() == (
            "history" if node == "existing" else ""
        )


def test_log_copy_failure_cleans_workspace_and_keeps_parent(
    repository: CandidateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日志复制失败不能留下半份可用 Workspace 或改变正式父级。"""
    import shutil

    seed = repository.initialize_seed()
    original_copy = shutil.copyfile

    def fail_log(
        source: Path, destination: Path, *, follow_symlinks: bool = True
    ) -> Path:
        if Path(source).name == "changelog.md":
            raise OSError("copy failed")
        original_copy(source, destination, follow_symlinks=follow_symlinks)
        return destination

    monkeypatch.setattr(shutil, "copyfile", fail_log)
    with pytest.raises(OSError, match="copy failed"):
        repository.create_workspace(seed.id, changelog_source_id=seed.id)
    assert repository.list_candidates() == (seed,)
    assert not list((seed.directory.parents[3] / ".evolution").glob(".workspace-*"))
    assert seed.changelog_path.read_text() == ""


def test_unreadable_log_aborts_publication_without_partial_candidate(
    repository: CandidateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日志无法读取时保留错误及草稿，不发布只有 Target 的版本。"""
    seed = repository.initialize_seed()
    workspace_root = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace_root / "target/prompt/system.md").write_text("changed")
    read_text = Path.read_text

    def fail_log(path: Path, *args: object, **kwargs: object) -> str:
        if path == workspace_root / "changelog.md":
            raise PermissionError("log denied")
        return read_text(path, encoding="utf-8")

    monkeypatch.setattr(Path, "read_text", fail_log)
    with pytest.raises(PermissionError, match="log denied"):
        repository.publish_workspace(
            seed.id, workspace_root, candidate_id=repository.allocate_candidate_id()
        )
    assert repository.list_candidates() == (seed,)
    assert list(seed.directory.parent.glob(".*.staging")) == []
    assert workspace_root.is_dir()
    assert seed.changelog_path.read_text() == ""


def test_manifest_omits_configuration_signatures(
    repository: CandidateRepository,
) -> None:
    """Manifest 展示配置，但不记录内容摘要签名。"""
    seed = repository.initialize_seed()

    manifest = json.loads(
        (seed.directory.parent.parent / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest == {
        "seed_candidate_id": seed.id,
        "objective": "improve",
        "read_only": ["data/", "reserved/"],
        "inaccessible": [],
    }


def test_seed_is_immutable_copy_of_initial_target(
    repository: CandidateRepository,
) -> None:
    """导入 Seed 不得修改或引用 initial_target。"""
    seed = repository.initialize_seed()
    source = repository.initial_target / "prompt" / "system.md"

    assert (seed.target_dir / "prompt" / "system.md").read_text() == "original\n"
    assert source.read_text() == "original\n"
    assert seed.id == "candidate-20260901T000000"
    assert seed.parent_id is None
    assert (seed.directory / "parent").read_text(encoding="utf-8") == ""


def test_seed_manifest_failure_removes_unpublished_candidate(
    repository: CandidateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manifest 未发布时不得遗留一个看似正式的 Seed。"""

    def fail_manifest(path: Path, value: JsonValue) -> None:
        if path.name == "manifest.json":
            raise OSError("disk full")
        real_atomic_write_json(path, value)

    monkeypatch.setattr(
        "evolution.candidates.atomic_write_json",
        fail_manifest,
    )

    with pytest.raises(OSError, match="disk full"):
        repository.initialize_seed()

    assert repository.list_candidates() == ()


def test_seed_restart_recovers_published_seed_without_manifest(
    repository: CandidateRepository,
    tmp_path: Path,
) -> None:
    """复现 Seed 已 rename、Manifest 尚未落盘的进程退出状态。"""
    seed = repository.initialize_seed()
    manifest = seed.directory.parent.parent / "manifest.json"
    manifest.unlink()
    reopened = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=repository.initial_target,
        objective="improve",
        read_only=("data/", "reserved/"),
        clock=_Clock(),
    )
    recovered = reopened.initialize_seed()
    assert recovered.id == seed.id
    assert reopened.list_candidates() == (seed,)
    assert json.loads(manifest.read_text())["seed_candidate_id"] == seed.id


def test_recovered_seed_survives_another_manifest_write_failure(
    repository: CandidateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恢复时只补 Manifest，写入再次失败不能删除已有 Seed。"""
    seed = repository.initialize_seed()
    manifest = seed.directory.parent.parent / "manifest.json"
    manifest.unlink()

    def fail_manifest(path: Path, value: JsonValue) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("evolution.candidates.atomic_write_json", fail_manifest)
    with pytest.raises(OSError, match="disk full"):
        repository.initialize_seed()
    assert repository.list_candidates() == (seed,)
    assert not manifest.exists()


def test_seed_restart_rejects_ambiguous_candidates(
    repository: CandidateRepository,
) -> None:
    """缺 Manifest 且已有多个候选时不得猜测归属或继续创建 Seed。"""
    seed, current = _rollback_chain(repository)
    (seed.directory.parent.parent / "manifest.json").unlink()
    with pytest.raises(ValueError, match="Seed"):
        repository.initialize_seed()
    assert repository.list_candidates() == (seed, current)


def test_publish_workspace_records_text_diff_and_parent(
    repository: CandidateRepository,
) -> None:
    """合法文本变化必须形成父关系和可读净 diff。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target" / "prompt" / "system.md").write_text(
        "improved\n", encoding="utf-8"
    )

    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )

    assert candidate is not None
    assert candidate.parent_id == seed.id
    change = collect_changes(seed.target_dir, candidate.target_dir)[0]
    assert change.path == "prompt/system.md"
    assert "-original" in (change.diff or "")
    assert "+improved" in (change.diff or "")


def test_candidate_ids_advance_by_whole_seconds_on_collision(tmp_path: Path) -> None:
    """同秒发布的 Candidate 必须保持纯秒级 ID，并选择下一个空闲秒。"""
    target = tmp_path / "initial"
    target.mkdir()
    (target / "prompt.md").write_text("seed\n", encoding="utf-8")
    now = datetime(2026, 9, 1).astimezone()
    repository = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="improve",
        read_only=(),
        clock=lambda: now,
    )
    seed = repository.initialize_seed()
    first_workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (first_workspace / "target" / "prompt.md").write_text("first\n", encoding="utf-8")
    first = repository.publish_workspace(
        seed.id, first_workspace, candidate_id=repository.allocate_candidate_id()
    )
    second_workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (second_workspace / "target" / "prompt.md").write_text("second\n", encoding="utf-8")
    second = repository.publish_workspace(
        seed.id, second_workspace, candidate_id=repository.allocate_candidate_id()
    )

    assert first is not None and second is not None
    assert [seed.id, first.id, second.id] == [
        "candidate-20260901T000000",
        "candidate-20260901T000001",
        "candidate-20260901T000002",
    ]


def test_diff_preserves_diff_boundaries_without_final_newlines(
    repository: CandidateRepository,
) -> None:
    """双方末行无换行时，删除与新增记录不得粘连。"""
    source = repository.initial_target / "prompt" / "system.md"
    source.write_text("old", encoding="utf-8")
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target" / "prompt" / "system.md").write_text("new", encoding="utf-8")
    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert candidate is not None

    content = collect_changes(seed.target_dir, candidate.target_dir)[0].diff or ""
    assert "-old\n\\ No newline at end of file\n+new\n" in content
    assert content.count("\\ No newline at end of file") == 2


def test_diff_preserves_trailing_spaces_in_diff(
    repository: CandidateRepository,
) -> None:
    """净 diff 不得删除实际变更行末的空格。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target" / "prompt" / "system.md").write_text(
        "new  \n", encoding="utf-8"
    )
    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert candidate is not None

    assert "+new  \n" in (
        collect_changes(seed.target_dir, candidate.target_dir)[0].diff or ""
    )


def test_diff_classifies_real_nul_content_as_binary(
    repository: CandidateRepository,
) -> None:
    """可解码但含 NUL 的文件也必须使用二进制元数据。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target" / "assets").mkdir()
    (workspace / "target" / "assets" / "model.bin").write_bytes(b"hello\x00world")
    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert candidate is not None
    assert collect_changes(seed.target_dir, candidate.target_dir)[0].diff is None

    assert collect_changes(seed.target_dir, candidate.target_dir)[0].after_bytes == 11


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="需要宿主时区切换支持")
@pytest.mark.parametrize(
    "zone,stamp",
    [
        ("UTC-8", "20260902T053000"),
        ("UTC5", "20260901T163000"),
    ],
)
def test_candidate_uses_host_timezone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zone: str,
    stamp: str,
) -> None:
    """同一个 UTC 时刻按宿主时区命名。"""
    target = tmp_path / "initial"
    target.mkdir()
    now = datetime(2026, 9, 1, 21, 30, tzinfo=UTC)
    repository = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="test",
        read_only=(),
        clock=lambda: now,
    )
    try:
        with monkeypatch.context() as context:
            context.setenv("TZ", zone)
            time.tzset()
            seed = repository.initialize_seed()
            assert seed.id == f"candidate-{stamp}"
    finally:
        time.tzset()


def test_publish_empty_workspace_removes_it_without_candidate(
    repository: CandidateRepository,
) -> None:
    """无净变化不得制造 Candidate 事实。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)

    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )

    assert candidate is None
    assert not workspace.exists()
    assert repository.list_candidates() == (seed,)


def test_publish_rejects_change_in_read_only(
    repository: CandidateRepository,
) -> None:
    """保护文件发生变化时必须拒绝发布并保留 Workspace。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target" / "data" / "db.json").write_text('{"changed": true}\n')

    with pytest.raises(ValueError, match="read_only"):
        repository.publish_workspace(
            seed.id, workspace, candidate_id=repository.allocate_candidate_id()
        )

    assert workspace.exists()


def test_publish_allows_new_path_outside_read_only(
    repository: CandidateRepository,
) -> None:
    """未保护的新文件默认允许发布。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target" / "plugin.py").write_text(
        "enabled = True\n", encoding="utf-8"
    )

    candidate = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )

    assert candidate is not None
    assert collect_changes(seed.target_dir, candidate.target_dir)[0].path == "plugin.py"


def test_publish_rejects_symlink_even_inside_mutable_path(
    repository: CandidateRepository,
    tmp_path: Path,
) -> None:
    """符号链接不得绕过 Workspace 的文件边界。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "target" / "prompt" / "link").symlink_to(outside)

    with pytest.raises(ValueError, match="符号链接"):
        repository.publish_workspace(
            seed.id, workspace, candidate_id=repository.allocate_candidate_id()
        )


def test_load_candidate_detects_external_target_tampering(
    repository: CandidateRepository,
) -> None:
    """正式 Target 的外部修改必须被摘要校验发现。"""
    seed = repository.initialize_seed()
    prompt = seed.target_dir / "prompt" / "system.md"
    os.chmod(prompt, 0o644)
    prompt.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="摘要"):
        repository.load_candidate(seed.id)


@pytest.mark.parametrize("role", [DatasetRole.TRAIN, DatasetRole.TEST])
def test_evaluation_message_layout_preserves_saved_data(
    repository: CandidateRepository, role: DatasetRole
) -> None:
    """各 Role 评测均使用紧凑消息排版，仍可无损加载。"""
    seed = repository.initialize_seed()
    evaluation = _evaluation()
    case = evaluation.cases[0].model_copy(
        update={
            "tracelog": {
                "simulation": {
                    "messages": [
                        {
                            "role": "tool",
                            "id": "call-1",
                            "error": False,
                            "content": '{"result": "中文\\n下一行"}',
                        }
                    ]
                }
            }
        }
    )
    evaluation = evaluation.model_copy(update={"cases": (case,)})
    repository.save_execution(seed.id, role, _execution())
    repository.save_evaluation(seed.id, role, evaluation)
    text = (seed.directory / role.value / "evaluation.json").read_text()
    assert '"role": "tool", "id": "call-1", "error": false,' in text
    assert json.loads(text) == evaluation.model_dump(mode="json")
    assert repository.load_evaluation(seed.id, role) == evaluation


def test_saving_execution_invalidates_previous_evaluation(
    repository: CandidateRepository,
) -> None:
    """重跑 Execution 后旧 Evaluation 不得继续可见。"""
    seed = repository.initialize_seed()
    repository.save_execution(seed.id, DatasetRole.TRAIN, _execution())
    repository.save_evaluation(seed.id, DatasetRole.TRAIN, _evaluation())

    repository.save_execution(seed.id, DatasetRole.TRAIN, _execution("task_002"))

    assert repository.load_execution(seed.id, DatasetRole.TRAIN) == _execution(
        "task_002"
    )
    assert repository.load_evaluation(seed.id, DatasetRole.TRAIN) is None


def test_evaluation_ids_must_match_saved_execution(
    repository: CandidateRepository,
) -> None:
    """Evaluator 不得发布遗漏或新增 Case 的评分。"""
    seed = repository.initialize_seed()
    repository.save_execution(seed.id, DatasetRole.TRAIN, _execution())

    with pytest.raises(ValueError, match="Case ID"):
        repository.save_evaluation(
            seed.id,
            DatasetRole.TRAIN,
            _evaluation("task_other"),
        )


@pytest.mark.parametrize(
    "damage", ["missing_execution", "wrong_cases", "directory", "json"]
)
def test_loaded_stage_damage_is_not_a_recovery_checkpoint(
    repository: CandidateRepository, damage: str
) -> None:
    """文件损坏或不一致不能伪装成缺失阶段。"""
    seed = repository.initialize_seed()
    repository.save_execution(seed.id, DatasetRole.TRAIN, _execution())
    repository.save_evaluation(seed.id, DatasetRole.TRAIN, _evaluation())
    execution_path = seed.directory / "train/execution.json"
    if damage == "missing_execution":
        execution_path.unlink()
    elif damage == "wrong_cases":
        execution_path.write_text(_execution("other").model_dump_json())
    elif damage == "directory":
        execution_path.unlink()
        execution_path.mkdir()
    else:
        execution_path.write_text("{broken")
    with pytest.raises((ValueError, OSError)):
        repository.load_evaluation(seed.id, DatasetRole.TRAIN)


def test_workspace_can_inherit_rejected_sibling_log(
    repository: CandidateRepository,
) -> None:
    """复制最优代码与最近日志，不修改正式日志。"""
    seed = repository.initialize_seed()
    workspace = repository.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "target/prompt/system.md").write_text("rejected")
    (workspace / "changelog.md").write_text("failed attempt\n")
    rejected = repository.publish_workspace(
        seed.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert rejected is not None
    workspace = repository.create_workspace(seed.id, changelog_source_id=rejected.id)
    assert (workspace / "target/prompt/system.md").read_text() == "original\n"
    assert (workspace / "changelog.md").read_text() == "failed attempt\n"
    (workspace / "changelog.md").write_text("backfilled\n")
    assert rejected.changelog_path.read_text() == "failed attempt\n"


def test_replace_test_result_keeps_previous_pair_when_staging_fails(
    repository: CandidateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新 Test 结果未完整写入时旧执行和评分必须同时保留。"""
    seed = repository.initialize_seed()
    repository.replace_test_result(seed.id, _execution(), _evaluation())

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "evolution.candidates.atomic_write_json",
        fail_write,
    )

    with pytest.raises(OSError, match="disk full"):
        repository.replace_test_result(
            seed.id,
            _execution("task_002"),
            _evaluation("task_002", 0.0),
        )

    assert repository.load_execution(seed.id, DatasetRole.TEST) == _execution()
    assert repository.load_evaluation(seed.id, DatasetRole.TEST) == _evaluation()


@pytest.mark.parametrize("permission", ("read_only", "inaccessible"))
@pytest.mark.parametrize("mutation", ("file", "remove_directory", "add_directory"))
def test_publish_rejects_restricted_changes(
    tmp_path: Path, permission: str, mutation: str
) -> None:
    """发布前检查必须独立于工具覆盖文件及空目录变化。"""
    target = tmp_path / "source"
    (target / "restricted/empty").mkdir(parents=True)
    (target / "restricted/file.txt").write_text("original")
    repo = CandidateRepository(
        tmp_path / "run",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="test",
        read_only=("restricted/",) if permission == "read_only" else (),
        inaccessible=("restricted/",) if permission == "inaccessible" else (),
    )
    seed = repo.initialize_seed()
    workspace = repo.create_workspace(seed.id, changelog_source_id=seed.id)
    assert (workspace / "target" / "restricted/file.txt").read_text() == "original"
    if mutation == "file":
        (workspace / "target" / "restricted/file.txt").write_text("changed")
    elif mutation == "remove_directory":
        (workspace / "target" / "restricted/empty").rmdir()
    else:
        (workspace / "target" / "restricted/new").mkdir()
    with pytest.raises(ValueError, match="边界"):
        repo.publish_workspace(
            seed.id, workspace, candidate_id=repo.allocate_candidate_id()
        )


def test_manifest_rejects_changed_access_policy(tmp_path: Path) -> None:
    """恢复实验不能悄悄更换权限策略。"""
    target = tmp_path / "source"
    target.mkdir()
    CandidateRepository(
        tmp_path / "run",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="test",
        read_only=(),
    ).initialize_seed()
    with pytest.raises(ValueError, match="不一致"):
        CandidateRepository(
            tmp_path / "run",
            workspace_root=tmp_path / ".evolution",
            initial_target=target,
            objective="test",
            read_only=(),
            inaccessible=("**/.*",),
        ).initialize_seed()


def test_discard_workspace_only_removes_its_own_directory(tmp_path: Path) -> None:
    """清理失败副本不删除父版本、相邻副本或链接指向的外部文件。"""
    target = tmp_path / "target"
    target.mkdir()
    (target / "prompt.md").write_text("seed")
    repo = CandidateRepository(
        tmp_path / "experiment",
        workspace_root=tmp_path / ".evolution",
        initial_target=target,
        objective="improve",
        read_only=(),
    )
    seed = repo.initialize_seed()
    workspace = repo.create_workspace(seed.id, changelog_source_id=seed.id)
    other = repo.create_workspace(seed.id, changelog_source_id=seed.id)
    (workspace / "external").symlink_to(target, target_is_directory=True)
    link = workspace.parent / "workspace-link"
    link.symlink_to(target, target_is_directory=True)
    for invalid in (
        target,
        workspace.parent,
        seed.target_dir,
        link,
        workspace / "external",
    ):
        with pytest.raises(ValueError):
            repo.discard_workspace(invalid)
    repo.discard_workspace(workspace)
    assert not workspace.exists()
    assert other.is_dir()
    assert (target / "prompt.md").read_text() == "seed"
    assert (seed.target_dir / "prompt.md").read_text() == "seed"
