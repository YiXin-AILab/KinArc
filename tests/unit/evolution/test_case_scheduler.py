"""逐 Case 调度和重试行为。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import Never

import pytest
from evolution.execution import run_cases
from evolution.models import CaseAttempt, CaseExecution, ExecutionMetadata


def _result(identifier: str, *, retryable: bool = False) -> CaseExecution:
    return CaseExecution(
        id=identifier,
        result="done",
        tracelog="",
        retryable=retryable,
        metadata=ExecutionMetadata(total_tokens=0, duration_seconds=0),
    )


def test_retry_aggregates_fractional_durations() -> None:
    """第三次尝试不得因逐次加法与 sum 的舍入差异丢失整批结果。"""
    durations = iter((0.1, 0.2, 0.3))

    def execute(case: str, stop: Event) -> CaseExecution:
        duration = next(durations)
        return CaseExecution(
            id=case,
            result="done",
            tracelog="",
            retryable=duration < 0.3,
            metadata=ExecutionMetadata(total_tokens=1, duration_seconds=duration),
        )

    result = run_cases(
        ("a",),
        case_id=str,
        execute_once=execute,
        max_case_concurrency=1,
        max_retries=3,
        retry_delay_seconds=0,
    ).cases[0]
    assert result.metadata.duration_seconds == 0.6
    assert result.metadata.total_tokens == 3
    assert len(result.attempts) == 3
    assert CaseExecution.model_validate_json(result.model_dump_json()) == result


def test_progress_counts_final_cases_not_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def execute(case: str, stop: Event) -> CaseExecution:
        nonlocal calls
        calls += 1
        return _result(case, retryable=case == "a")

    with caplog.at_level("INFO", logger="evolution.execution"):
        run_cases(
            ("a", "b"),
            case_id=str,
            execute_once=execute,
            max_case_concurrency=1,
            max_retries=1,
            retry_delay_seconds=0,
        )
    messages = [record.getMessage() for record in caplog.records]
    assert calls == 3
    assert (
        messages.count(
            "case_retry case=a attempt=2 max_attempts=2 result=done duration=0.000"
        )
        == 1
    )
    assert [
        message for message in messages if not message.startswith("case_retry")
    ] == [
        "case_dispatched case=a index=1/2 completed=0/2 active=1",
        "case_completed case=a result=done attempts=2 completed=1/2 active=0",
        "case_dispatched case=b index=2/2 completed=1/2 active=1",
        "case_completed case=b result=done attempts=1 completed=2/2 active=0",
    ]


@pytest.mark.parametrize("malformed", ["id", "attempts"])
def test_invalid_single_attempt_is_fatal(malformed: str) -> None:
    def execute(case: str, stop: Event) -> CaseExecution:
        result = _result("wrong" if malformed == "id" else case)
        if malformed == "attempts":
            return result.model_copy(
                update={
                    "attempts": (
                        CaseAttempt(
                            result="done", tracelog="", metadata=result.metadata
                        ),
                    )
                }
            )
        return result

    with pytest.raises(ValueError, match="单次执行"):
        run_cases(
            ("a",),
            case_id=str,
            execute_once=execute,
            max_case_concurrency=1,
            max_retries=3,
            retry_delay_seconds=0,
        )


def test_empty_input_does_not_create_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        pytest.fail("no workers for empty input")

    monkeypatch.setattr("evolution.execution.ThreadPoolExecutor", forbidden)
    assert (
        run_cases(
            (),
            case_id=str,
            execute_once=forbidden,
            max_case_concurrency=1,
            max_retries=0,
            retry_delay_seconds=0,
        ).cases
        == ()
    )


def test_cancel_during_retry_delay_does_not_retry() -> None:
    external, completed = Event(), Event()
    calls = []

    def execute(case: str, stop: Event) -> CaseExecution:
        calls.append(case)
        completed.set()
        return _result(case, retryable=True)

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            run_cases,
            ("a", "b"),
            case_id=str,
            execute_once=execute,
            max_case_concurrency=1,
            max_retries=3,
            retry_delay_seconds=30,
            cancel_event=external,
        )
        try:
            assert completed.wait(2)
        finally:
            external.set()
        with pytest.raises(asyncio.CancelledError):
            future.result(2)
    assert calls == ["a"]


def test_completed_slot_is_refilled_before_slow_case_finishes() -> None:
    a_started, c_started = Event(), Event()

    def execute(case: str, stop: Event) -> CaseExecution:
        if case == "a":
            a_started.set()
            assert c_started.wait(2)
        elif case == "b":
            assert a_started.wait(2)
        else:
            c_started.set()
        return _result(case)

    result = run_cases(
        ("a", "b", "c"),
        case_id=str,
        execute_once=execute,
        max_case_concurrency=2,
        max_retries=0,
        retry_delay_seconds=0,
    )
    assert [case.id for case in result.cases] == ["a", "b", "c"]


def test_retry_does_not_wait_for_other_cases() -> None:
    """B 必须等 A 重试才能完成，且同一 case 不得重叠。"""
    b_started, a_retried = Event(), Event()
    lock = Lock()
    active: set[str] = set()
    calls: dict[str, int] = {}

    def execute(case: str, stop: Event) -> CaseExecution:
        with lock:
            assert case not in active
            active.add(case)
            assert len(active) <= 2
            calls[case] = calls.get(case, 0) + 1
            attempt = calls[case]
        try:
            if case == "A" and attempt == 1:
                assert b_started.wait(2)
            elif case == "A":
                a_retried.set()
            elif case == "B":
                b_started.set()
                assert a_retried.wait(2)
            return CaseExecution(
                id=case,
                result=str(attempt),
                tracelog="",
                metadata=ExecutionMetadata(total_tokens=0, duration_seconds=0),
                retryable=case == "A" and attempt == 1,
            )
        finally:
            with lock:
                active.remove(case)

    result = run_cases(
        ("A", "B", "C"),
        case_id=str,
        execute_once=execute,
        max_case_concurrency=2,
        max_retries=1,
        retry_delay_seconds=0,
    )
    assert [case.id for case in result.cases] == ["A", "B", "C"]
    assert calls == {"A": 2, "B": 1, "C": 1}
    assert len(result.cases[0].attempts) == 2


@pytest.mark.parametrize(
    "retries,failures,expected", [(0, 5, 1), (3, 2, 3), (2, 5, 3), (3, 0, 1)]
)
def test_retry_budget_and_aggregation(
    retries: int,
    failures: int,
    expected: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """完成日志报告最后一次结果及包含首次执行的总尝试次数。"""
    caplog.set_level("INFO", logger="evolution.execution")
    calls = 0

    def execute(case: str, stop: Event) -> CaseExecution:
        nonlocal calls
        calls += 1
        return CaseExecution(
            id=case,
            result="agent_error" if calls <= failures else "completed",
            tracelog="",
            retryable=calls <= failures,
            metadata=ExecutionMetadata(
                duration_seconds=calls + 1, total_tokens=calls * 10
            ),
        )

    result = run_cases(
        ("a",),
        case_id=str,
        execute_once=execute,
        max_case_concurrency=1,
        max_retries=retries,
        retry_delay_seconds=0,
    ).cases[0]
    assert calls == expected
    final_result = "agent_error" if expected <= failures else "completed"
    assert result.result == final_result
    assert [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("case_completed")
    ] == [
        f"case_completed case=a result={final_result} attempts={expected} "
        "completed=1/1 active=0"
    ]
    assert len(result.attempts) == (expected if expected > 1 else 0)
    assert result.metadata.total_tokens == sum(i * 10 for i in range(1, expected + 1))
    assert result.metadata.duration_seconds == sum(
        i + 1 for i in range(1, expected + 1)
    )


@pytest.mark.parametrize(
    "cases,concurrency,retries,delay",
    [
        (("a", "a"), 1, 0, 0),
        ((" ",), 1, 0, 0),
        (("a",), True, 0, 0),
        (("a",), 0, 0, 0),
        (("a",), 1, True, 0),
        (("a",), 1, -1, 0),
        (("a",), 1, 0, float("inf")),
        (("a",), 1, 0, float("nan")),
    ],
)
def test_invalid_input_has_no_side_effects(
    cases: tuple[str, ...], concurrency: int, retries: int, delay: float
) -> None:
    def execute(case: str, stop: Event) -> Never:
        pytest.fail("must validate before execution")

    with pytest.raises(ValueError):
        run_cases(
            cases,
            case_id=str,
            execute_once=execute,
            max_case_concurrency=concurrency,
            max_retries=retries,
            retry_delay_seconds=delay,
        )


def test_fatal_error_drains_workers_without_setting_external_event() -> None:
    started, cleaned, external = Event(), Event(), Event()

    def execute(case: str, stop: Event) -> CaseExecution:
        if case == "a":
            assert started.wait(2)
            raise ValueError("fatal")
        started.set()
        assert stop.wait(2)
        cleaned.set()
        raise asyncio.CancelledError

    with pytest.raises(ValueError, match="fatal"):
        run_cases(
            ("a", "b", "c"),
            case_id=str,
            execute_once=execute,
            max_case_concurrency=2,
            max_retries=3,
            retry_delay_seconds=0,
            cancel_event=external,
        )
    assert cleaned.is_set()
    assert not external.is_set()


def test_continue_on_error_preserves_peers_and_runs_remaining_cases() -> None:
    peer_started, next_started = Event(), Event()
    calls: list[str] = []
    completed: list[str] = []

    def execute(case: str, stop: Event) -> CaseExecution:
        calls.append(case)
        if case == "a":
            assert peer_started.wait(2)
            raise RuntimeError("provider timeout")
        if case == "b":
            peer_started.set()
            # The next case is dispatched only after the failing one releases its slot.
            assert next_started.wait(2)
        else:
            next_started.set()
        assert not stop.is_set()
        completed.append(case)
        return _result(case)

    with pytest.raises(RuntimeError, match="provider timeout"):
        run_cases(
            ("a", "b", "c"),
            case_id=str,
            execute_once=execute,
            max_case_concurrency=2,
            max_retries=0,
            retry_delay_seconds=0,
            continue_on_error=True,
        )
    assert sorted(calls) == ["a", "b", "c"]
    assert sorted(completed) == ["b", "c"]


def test_continue_on_error_still_honors_external_cancellation() -> None:
    external, cleaned = Event(), Event()
    calls: list[str] = []

    def execute(case: str, stop: Event) -> CaseExecution:
        calls.append(case)
        external.set()
        assert stop.wait(2)
        cleaned.set()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run_cases(
            ("a", "b"),
            case_id=str,
            execute_once=execute,
            max_case_concurrency=1,
            max_retries=0,
            retry_delay_seconds=0,
            cancel_event=external,
            continue_on_error=True,
        )
    assert cleaned.is_set()
    assert calls == ["a"]
