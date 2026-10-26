"""Evolution Run 注册表（server.platform.evolution.runner）单元测试。"""

import time
from pathlib import Path

import pytest
from server.platform.evolution.runner import (
    EvolutionRunRegistry,
    RunStatus,
    RunView,
    parse_progress_message,
)
from server.platform.evolution.store import EvolutionStore
from tests.unit.api.evolution_helpers import (
    FakeImprover,
    fake_improver_factory,
    write_runnable_task,
)


def _registry(root: Path, improver: FakeImprover) -> EvolutionRunRegistry:
    store = EvolutionStore(
        project_root=root,
        tasks_root=root / "evolution_tasks",
    )
    return EvolutionRunRegistry(
        project_root=root,
        store=store,
        improver_factory=fake_improver_factory(improver),
    )


def _wait_status(
    registry: EvolutionRunRegistry,
    run_id: str,
    *statuses: RunStatus,
    timeout: float = 15,
) -> RunView:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = registry.get(run_id)
        if view.status in statuses:
            return view
        time.sleep(0.05)
    raise AssertionError(f"Run 未在 {timeout}s 内进入 {statuses}")


def test_parse_progress_message_structured() -> None:
    event, fields = parse_progress_message(
        "run_started mode=initialize max_iterations=5"
    )
    assert event == "run_started"
    assert fields == {"mode": "initialize", "max_iterations": "5"}
    event, fields = parse_progress_message(
        "case_completed case=c1 result=ok attempts=1 completed=2/8 active=1"
    )
    assert event == "case_completed"
    assert fields["completed"] == "2/8"


def test_parse_progress_message_falls_back_to_log() -> None:
    event, fields = parse_progress_message("Changelog 缺少迭代版本，跳过回填：x")
    assert event == "log"
    assert fields == {}


def test_full_run_completes_with_progress_events(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    registry = _registry(tmp_path, FakeImprover())
    view = registry.start("tpl")
    final = _wait_status(registry, view.run_id, RunStatus.COMPLETED)
    assert final.stop_reason == "iteration_limit"
    assert final.best_candidate_id is not None
    assert final.validation_score == 1.0
    events = registry.events_after(view.run_id, 0)
    names = [event.event for event in events]
    assert "run_started" in names
    assert "iteration_started" in names
    assert "improver_attempt_started" in names
    assert "candidate_published" in names
    assert "case_completed" in names
    assert "stage_completed" in names
    assert "run_completed" in names
    assert [event.cursor for event in events] == list(range(1, len(events) + 1))


def test_second_start_conflicts_and_stop_terminates(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    registry = _registry(tmp_path, FakeImprover(delay_seconds=30))
    view = registry.start("tpl")
    with pytest.raises(RuntimeError, match="已有运行中的进化任务"):
        registry.start("tpl")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(
            event.event == "improver_attempt_started"
            for event in registry.events_after(view.run_id, 0)
        ):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Improver 未开始")
    registry.stop(view.run_id)
    final = _wait_status(registry, view.run_id, RunStatus.STOPPED)
    assert final.finished_at is not None
    names = [event.event for event in registry.events_after(view.run_id, 0)]
    assert "run_stopped" in names


def test_failed_run_records_error(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    (tmp_path / "evolution_tasks" / "tpl" / "executor.py").write_text(
        "def create_executor(task_dir, settings):\n    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    registry = _registry(tmp_path, FakeImprover())
    view = registry.start("tpl")
    final = _wait_status(registry, view.run_id, RunStatus.FAILED)
    assert final.error is not None
    names = [event.event for event in registry.events_after(view.run_id, 0)]
    assert "run_failed" in names
