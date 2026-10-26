"""Attempt ownership covers construction, execution, and grading errors."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from benchmarks.tau2bench.config import IntegrationConfig
from benchmarks.tau2bench.lifecycle import (
    AttemptContext,
    BatchContext,
    attempt_scope,
    bound_attempt,
    current_attempt,
    native_batch,
    request_timeout,
)


@pytest.fixture
def batch(tmp_path: Path) -> BatchContext:
    """Minimal explicit configuration, with no Runtime or external I/O."""
    config = IntegrationConfig(
        agent_dir=tmp_path,
        runtime_config=tmp_path / "runtime.yaml",
        runtime_python=tmp_path / "python",
    )
    return BatchContext(config, tmp_path, "test", 0, 1)


def test_thread_context_and_cleanup(batch: BatchContext) -> None:
    """Native worker threads never share an attempt context."""
    barrier = Barrier(2)
    closed: list[str] = []

    def work(identity: str) -> str:
        context = AttemptContext(identity, "task", None, batch, Event())
        context.own(lambda: closed.append(identity))
        with bound_attempt(context):
            barrier.wait(timeout=2)
            assert current_attempt() is context
            return current_attempt().simulation_id

    with ThreadPoolExecutor(2) as pool:
        assert set(pool.map(work, ["a", "b"])) == {"a", "b"}
    assert set(closed) == {"a", "b"}
    with pytest.raises(RuntimeError):
        current_attempt()


def test_failure_closes_every_resource(batch: BatchContext) -> None:
    """One failed close does not skip the other owned resources."""
    closed: list[int] = []
    context = AttemptContext("a", "task", None, batch, Event())
    context.own(lambda: closed.append(1))

    def fail() -> None:
        raise RuntimeError("close failed")

    context.own(fail)
    with pytest.raises(ExceptionGroup), bound_attempt(context):
        raise ValueError("initialize failed")
    assert closed == [1]
    assert context.cancel.is_set()


def test_cleanup_failure_does_not_cancel_other_attempts(
    batch: BatchContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attempt cannot interrupt an active sibling or prevent a retry."""
    config = SimpleNamespace(effective_agent="agentsystem_banking")
    monkeypatch.setattr("benchmarks.tau2bench.lifecycle._batches", {id(config): batch})
    active = Event()
    failed = Event()

    def sibling() -> None:
        with attempt_scope(config, SimpleNamespace(id="sibling"), "b", 1, None):
            context = current_attempt()
            active.set()
            assert failed.wait(5)
            assert not context.cancel.is_set()
            context.own(lambda: None)

    def broken() -> None:
        assert active.wait(5)

        def fail() -> None:
            raise RuntimeError("injected cleanup failure")

        try:
            with (
                pytest.raises(ExceptionGroup),
                attempt_scope(config, SimpleNamespace(id="broken"), "a", 1, None),
            ):
                current_attempt().own(fail)
        finally:
            failed.set()

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(sibling), pool.submit(broken)]
        for future in futures:
            future.result(timeout=10)
    with attempt_scope(config, SimpleNamespace(id="broken"), "retry", 1, None):
        assert not current_attempt().cancel.is_set()


@pytest.mark.parametrize("stage", ["build", "initialize", "grading"])
def test_attempt_failure_still_closes(batch: BatchContext, stage: str) -> None:
    """The final ownership scope covers failures outside Orchestrator.cleanup."""
    closed: list[str] = []
    context = AttemptContext("a", "task", None, batch, Event())
    with pytest.raises(ValueError, match=stage), bound_attempt(context):
        context.own(lambda: closed.append(stage))
        raise ValueError(stage)
    assert closed == [stage]
    assert not context.cancel.is_set()


def test_official_agent_bypasses_integration() -> None:
    """Official agents do not require our config, source or resource scopes."""
    config = SimpleNamespace(effective_agent="llm_agent")
    wrapped = native_batch(lambda value: value)
    assert wrapped(config) is config
    with attempt_scope(config, None, "native", 1, None), pytest.raises(RuntimeError):
        current_attempt()


def test_attempt_logs_share_results_without_retry_collisions(
    batch: BatchContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries retain distinct files in the official results directory."""
    config = SimpleNamespace(effective_agent="agentsystem_banking")
    monkeypatch.setattr("benchmarks.tau2bench.lifecycle._batches", {id(config): batch})
    paths = []
    for _ in range(2):
        with attempt_scope(config, SimpleNamespace(id="task_006"), "sim", 1, tmp_path):
            paths.append(current_attempt().log_path)
    assert paths[0] != paths[1]
    for path in paths:
        assert path is not None
        assert path.parent == tmp_path.resolve()
        assert path.name.startswith("runtime-task_006-sim-")


@pytest.mark.parametrize(
    ("args", "expected"),
    [({}, 30), ({"timeout": 120}, 120), ({"timeout": 5}, 5), ({"timeout": 2000}, 1200)],
)
def test_explicit_request_timeout(args: dict[str, object], expected: float) -> None:
    """Keep valid native settings, bounded only by the case budget."""
    assert request_timeout(args, default=30, case_timeout=1200) == expected


@pytest.mark.parametrize("value", [None, 0, -1, True, float("inf"), "30"])
def test_invalid_explicit_timeout_rejected(value: object) -> None:
    """Explicit invalid values cannot silently become a different timeout."""
    with pytest.raises(ValueError):
        request_timeout({"timeout": value}, default=30, case_timeout=1200)
