"""Evolution 本地 CLI 测试。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from agentsystem.config.models import RuntimeTraceConfig
from evolution.cli import (
    _dispatch,
    _execute_command,
    _format_result,
    _new_engine,
    build_parser,
    run_cli,
)
from evolution.engine import EvolutionEngine
from evolution.models import (
    OptimizationResult,
    StopReason,
)
from evolution.workspace import WorkspaceAccess


def _optimization_result() -> OptimizationResult:
    return OptimizationResult(
        seed_candidate_id="seed",
        best_candidate_id="best",
        best_target_dir=Path("/tmp/best/target"),
        iterations_attempted=1,
        candidates_created=1,
        validation_score=0.8,
        stop_reason=StopReason.ITERATION_LIMIT,
        changelog_paths=(Path("/tmp/best/changelog.md"),),
    )


def test_new_engine_receives_retry_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 必须把 Task 的重试配置注入 Engine。"""
    captured: dict[str, object] = {}

    def build_engine(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("evolution.cli.EvolutionEngine", build_engine)
    config = SimpleNamespace(
        objective="score",
        workspace=WorkspaceAccess(),
        max_iterations=5,
        max_retries=4,
        max_case_concurrency=2,
        single_case_min_cases=3,
        retry_delay_seconds=2.5,
        rollback_enabled=False,
        exploration_patience=2,
        rollback_changelog_source="latest",
    )

    _new_engine(
        repository=object(),  # type: ignore[arg-type]
        task=object(),  # type: ignore[arg-type]
        improver=object(),  # type: ignore[arg-type]
        config=config,  # type: ignore[arg-type]
    )

    assert captured["max_retries"] == 4
    assert captured["max_case_concurrency"] == 2
    assert captured["single_case_min_cases"] == 3
    assert captured["retry_delay_seconds"] == 2.5
    assert captured["rollback_enabled"] is False
    assert captured["exploration_patience"] == 2
    assert captured["rollback_changelog_source"] == "latest"


def test_parser_reads_run_command_and_task_directory() -> None:
    """CLI 将用户指定的任务目录传给 run 命令。"""
    parser = build_parser()

    arguments = parser.parse_args(["run", "experiment"])

    assert arguments.command == "run"
    assert arguments.task_dir == Path("experiment")


@pytest.mark.parametrize("command", ["rerun", "reevaluate"])
def test_parser_rejects_removed_commands(command: str) -> None:
    """已移除的命令必须在加载任务和修改结果之前被拒绝。"""
    parser = build_parser()

    with pytest.raises(SystemExit) as error:
        parser.parse_args([command, "experiment", "candidate", "train"])
    assert error.value.code == 2


async def test_dispatch_invokes_run() -> None:
    """Run 命令执行正常优化流程。"""
    arguments = build_parser().parse_args(["run", "experiment"])
    engine = create_autospec(EvolutionEngine, instance=True, spec_set=True)
    engine.run.return_value = _optimization_result()

    result = await _dispatch(arguments, engine)

    engine.run.assert_awaited_once_with()
    assert isinstance(result, OptimizationResult)


def test_parser_accepts_test_without_role_selection() -> None:
    """手动评测固定为 Test，不能借此重跑 Train 或 Validation。"""
    parser = build_parser()
    arguments = parser.parse_args(["test", "experiment", "candidate-a"])
    assert arguments.task_dir == Path("experiment")
    assert arguments.candidate_id == "candidate-a"
    with pytest.raises(SystemExit):
        parser.parse_args(["test", "experiment", "candidate-a", "train"])


async def test_run_uses_task_improver_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 命令必须使用当前 Evolution Task 的请求总超时。"""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.touch()
    config = SimpleNamespace(
        root=task_dir,
        initial_target=task_dir / "target",
        objective="score",
        workspace=WorkspaceAccess(read_only=("data/",)),
        max_iterations=5,
        improver_timeout_seconds=987,
    )
    engine = create_autospec(EvolutionEngine, instance=True, spec_set=True)
    engine.run.return_value = _optimization_result()
    captured: dict[str, object] = {}

    class Improver:
        async def __aenter__(self) -> "Improver":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    async def create_improver(**kwargs: object) -> Improver:
        captured.update(kwargs)
        return Improver()

    monkeypatch.setattr(
        "evolution.cli.load_task_config", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        "evolution.cli.load_task", lambda _: SimpleNamespace(digest="a" * 64)
    )
    monkeypatch.setattr("evolution.cli.CandidateRepository", lambda *_, **__: object())
    monkeypatch.setattr(
        "evolution.cli.load_yaml_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            logging=object(), trace=RuntimeTraceConfig(enabled=False)
        ),
    )
    monkeypatch.setattr(
        "evolution.cli.setup_logging",
        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        "evolution.cli.AgentSystemImprover.create",
        create_improver,
    )
    monkeypatch.setattr("evolution.cli._new_engine", lambda **_: engine)

    arguments = build_parser().parse_args(
        ["run", str(task_dir), "--runtime-config", str(runtime_path)]
    )
    result = await _execute_command(arguments, tmp_path, {})

    assert isinstance(result, OptimizationResult)
    assert captured["request_timeout_seconds"] == 987


def test_run_cli_reports_invalid_experiment_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """预期配置错误应成为简洁非零 CLI 结果。"""
    missing = tmp_path / "missing"

    exit_code = run_cli(
        ["run", str(missing)],
        project_root=tmp_path,
        environ={},
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "missing" in captured.err
    assert "Traceback" not in captured.err


def test_optimization_output_includes_changelog_paths() -> None:
    """CLI 优化摘要必须返回本次新建 Candidate 的 Changelog 路径。"""
    result = OptimizationResult(
        seed_candidate_id="seed",
        best_candidate_id="best",
        best_target_dir=Path("/tmp/best/target"),
        iterations_attempted=1,
        candidates_created=1,
        validation_score=0.8,
        stop_reason=StopReason.ITERATION_LIMIT,
        changelog_paths=(Path("/tmp/best/changelog.md"),),
    )

    output = json.loads(_format_result(result))

    assert output["changelog_paths"] == ["/tmp/best/changelog.md"]


def test_cli_reports_improver_exhaustion_without_unexpected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """优化器耗尽是正常停止结果，而非 CLI 未预期异常。"""
    result = OptimizationResult(
        seed_candidate_id="seed",
        best_candidate_id="seed",
        best_target_dir=tmp_path / "target",
        iterations_attempted=1,
        candidates_created=0,
        validation_score=0.2,
        stop_reason=StopReason.IMPROVER_FAILED,
    )

    async def execute(*args: object, **kwargs: object) -> OptimizationResult:
        return result

    monkeypatch.setattr("evolution.cli._execute_command", execute)
    assert run_cli(["run", str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert "improver_failed" in output.out
    assert "unexpected failure" not in output.err
