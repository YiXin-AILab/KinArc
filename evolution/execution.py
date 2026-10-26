"""Case 执行调度、评分比较与评测结果排版。"""

import asyncio
import json
import logging
import math
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Event

from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import JsonValue

from evolution.models import (
    CaseAttempt,
    CaseExecution,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
    ParentComparison,
)

_LOGGER = logging.getLogger(__name__)


def run_cases[CaseT](
    cases: Sequence[CaseT],
    *,
    case_id: Callable[[CaseT], str],
    execute_once: Callable[[CaseT, Event], CaseExecution],
    max_case_concurrency: int,
    max_retries: int,
    retry_delay_seconds: float,
    cancel_event: Event | None = None,
    continue_on_error: bool = False,
) -> ExecutionResult:
    """有界执行并按输入顺序返回结果，异常或取消时先回收全部执行者。

    只重试标记为 retryable 的结果；默认逃逸异常终止整批。continue_on_error 时普通异常
    留到整批结束后抛出，不取消其他 case，也不返回不完整结果。外部取消仍立即停止。
    case_completed 日志包含最终 result 和含首次执行的 attempts，不代表执行成功。

    Args:
        cases: 待执行 Case 序列，允许为空。
        case_id: 提取非空且唯一 Case ID 的函数。
        execute_once: 执行一次 Case 的函数，必须在退出前回收本次资源。
            第二个参数为内部停止信号；返回值不得带已有 attempts。
        max_case_concurrency: 同时占用的 Case 执行槽位上限。
        max_retries: 首次执行后的重试次数上限。
        retry_delay_seconds: 重试间隔，等待期间响应停止信号。
        cancel_event: 外部取消信号；调度器只读取、不修改它。
        continue_on_error: 普通异常发生后是否继续执行其他 Case。

    Returns:
        输入顺序的完整结果，包含重试历史和累计消耗。

    Raises:
        ValueError: 策略、Case ID 或单次执行结果不符合约定。
        asyncio.CancelledError: 外部取消或执行者传播取消。
    """
    validate_execution_policy(max_case_concurrency, max_retries, retry_delay_seconds)
    identified = tuple((case_id(case), case) for case in cases)
    ids = tuple(identifier for identifier, _ in identified)
    if any(
        not isinstance(identifier, str) or not identifier.strip() for identifier in ids
    ):
        raise ValueError("Case id 必须是非空字符串")
    if len(set(ids)) != len(ids):
        raise ValueError("Case id 重复")
    _check_cancelled(cancel_event)
    if not identified:
        return ExecutionResult(cases=())
    stop = Event()

    def execute(identifier: str, case: CaseT) -> CaseExecution:
        """执行单个 Case；遇到异常时通知同批执行者停止。"""
        try:
            return _run_case(
                case, identifier, execute_once, stop, max_retries, retry_delay_seconds
            )
        except Exception:
            if not continue_on_error:
                stop.set()
            raise
        except BaseException:
            stop.set()
            raise

    results = _schedule(
        identified, execute, max_case_concurrency, stop, cancel_event, continue_on_error
    )
    return ExecutionResult(cases=tuple(results[identifier] for identifier in ids))


def validate_execution_policy(concurrency: int, retries: int, delay: float) -> None:
    """在产生副作用前验证并发和重试参数。"""
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("max_case_concurrency 必须为正整数")
    if type(retries) is not int or retries < 0:
        raise ValueError("max_retries 必须为非负整数")
    if (
        isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not math.isfinite(delay)
        or delay < 0
    ):
        raise ValueError("retry_delay_seconds 必须为有限非负数")


def _schedule[CaseT](
    identified: tuple[tuple[str, CaseT], ...],
    execute: Callable[[str, CaseT], CaseExecution],
    concurrency: int,
    stop: Event,
    external: Event | None,
    continue_on_error: bool,
) -> dict[str, CaseExecution]:
    """有界派发 Case，收集结果；故障或取消时停止派发并等待线程退出。"""
    results: dict[str, CaseExecution] = {}
    pending: dict[Future[CaseExecution], str] = {}
    remaining = enumerate(identified, start=1)
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=min(concurrency, len(identified))) as pool:
        try:
            while True:
                if external is not None and external.is_set():
                    stop.set()
                while not stop.is_set() and len(pending) < concurrency:
                    item = next(remaining, None)
                    if item is None:
                        break
                    index, (identifier, case) = item
                    pending[pool.submit(execute, identifier, case)] = identifier
                    _LOGGER.info(
                        "case_dispatched case=%s index=%s/%s completed=%s/%s active=%s",
                        identifier,
                        index,
                        len(identified),
                        len(results),
                        len(identified),
                        len(pending),
                    )
                if not pending:
                    break
                done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    identifier = pending.pop(future)
                    try:
                        results[identifier] = future.result()
                        _LOGGER.info(
                            "case_completed case=%s result=%s attempts=%s "
                            "completed=%s/%s active=%s",
                            identifier,
                            results[identifier].result,
                            len(results[identifier].attempts) or 1,
                            len(results),
                            len(identified),
                            len(pending),
                        )
                    except BaseException as error:
                        errors.append(error)
                        if not continue_on_error or not isinstance(error, Exception):
                            stop.set()
                        else:
                            _LOGGER.warning(
                                "case_failed case=%s error_type=%s "
                                "action=continue_batch",
                                identifier,
                                type(error).__name__,
                            )
        finally:
            stop.set()
    _raise_errors(errors, external)
    return results


def _raise_errors(errors: list[BaseException], external: Event | None) -> None:
    """优先传播外部取消或主异常，并记录不会再被传播的普通异常。"""
    ordinary = [error for error in errors if isinstance(error, Exception)]
    # 主异常交给调用边界记录；其他失败不会覆盖主异常，也不能静默丢失。
    cancelled = external is not None and external.is_set()
    for error in ordinary if cancelled else ordinary[1:]:
        log_unexpected_exception(
            _LOGGER, "case_cleanup_failed", error, operation="evolution.execution"
        )
    _check_cancelled(external)
    if ordinary:
        raise ordinary[0]
    if errors:
        raise errors[0]


def _run_case[CaseT](
    case: CaseT,
    identifier: str,
    execute: Callable[[CaseT, Event], CaseExecution],
    stop: Event,
    retries: int,
    delay: float,
) -> CaseExecution:
    """在单个并发槽位内有限重试，聚合历史尝试并响应停止信号。"""
    aggregate: CaseExecution | None = None
    for attempt in range(retries + 1):
        _check_cancelled(stop)
        current = execute(case, stop)
        if current.id != identifier or current.attempts:
            raise ValueError("单次执行必须返回匹配的 Case id 且 attempts 为空")
        aggregate = (
            current if aggregate is None else _merge_case_attempt(aggregate, current)
        )
        if not current.retryable or attempt == retries:
            return aggregate
        _LOGGER.info(
            "case_retry case=%s attempt=%s max_attempts=%s result=%s duration=%.3f",
            identifier,
            attempt + 2,
            retries + 1,
            current.result,
            current.metadata.duration_seconds,
        )
        if stop.wait(delay):
            raise asyncio.CancelledError
    raise AssertionError("unreachable retry budget")


def _check_cancelled(event: Event | None) -> None:
    """将协作停止信号转换为 asyncio.CancelledError。"""
    if event is not None and event.is_set():
        raise asyncio.CancelledError


def _merge_case_attempt(
    previous: CaseExecution, current: CaseExecution
) -> CaseExecution:
    """保留全部尝试及消耗，以最新尝试的结果和轨迹代表 Case。"""
    attempts = previous.attempts or (_to_attempt(previous),)
    attempts = (*attempts, _to_attempt(current))
    return CaseExecution(
        id=current.id,
        result=current.result,
        tracelog=current.tracelog,
        metadata=ExecutionMetadata(
            total_tokens=previous.metadata.total_tokens + current.metadata.total_tokens,
            duration_seconds=sum(item.metadata.duration_seconds for item in attempts),
        ),
        retryable=current.retryable,
        attempts=attempts,
    )


def _to_attempt(case: CaseExecution) -> CaseAttempt:
    """从 Case 执行中提取一次尝试的结果、轨迹与消耗。"""
    return CaseAttempt(
        result=case.result, tracelog=case.tracelog, metadata=case.metadata
    )


def compare_evaluations(
    previous_candidate_id: str,
    previous: EvaluationResult,
    current_candidate_id: str,
    current: EvaluationResult,
) -> ParentComparison:
    """比较父子 Train 分数及逐题变化。"""
    previous_by_id = {case.id: case.score for case in previous.cases}
    current_by_id = {case.id: case.score for case in current.cases}
    improved: int | None = None
    regressed: int | None = None
    unchanged: int | None = None
    if previous_by_id.keys() == current_by_id.keys():
        improved = sum(
            current_by_id[case_id] > score for case_id, score in previous_by_id.items()
        )
        regressed = sum(
            current_by_id[case_id] < score for case_id, score in previous_by_id.items()
        )
        unchanged = len(previous_by_id) - improved - regressed
    delta = current.total_score - previous.total_score
    return ParentComparison(
        previous_candidate_id=previous_candidate_id,
        current_candidate_id=current_candidate_id,
        previous_total_score=previous.total_score,
        current_total_score=current.total_score,
        total_score_delta=delta,
        previous_case_count=len(previous.cases),
        current_case_count=len(current.cases),
        improved_case_count=improved,
        regressed_case_count=regressed,
        unchanged_case_count=unchanged,
    )


_MESSAGE_FIELDS = ("reasoning", "content", "tool_calls")


def format_evaluation_json(value: JsonValue) -> str:
    """保持 JSON 值不变，将消息属性分组、正文和工具调用各排成一行。"""
    return _format(value, 0)


def _format(value: JsonValue, depth: int) -> str:
    """递归排版 JSON，在消息对象处应用紧凑的属性分组。"""
    if isinstance(value, dict) and value:
        opening, closing = "{", "}"
        if isinstance(value.get("role"), str):
            entries = _message_entries(value)
        else:
            entries = [
                f"{_inline(key)}: {_format(item, depth + 1)}"
                for key, item in value.items()
            ]
    elif isinstance(value, list) and value:
        opening, closing = "[", "]"
        entries = [_format(item, depth + 1) for item in value]
    else:
        return _inline(value)
    body = ",\n".join("  " * (depth + 1) + entry for entry in entries)
    return opening + "\n" + body + "\n" + "  " * depth + closing


def _message_entries(message: dict[str, JsonValue]) -> list[str]:
    """将消息元属性合为一行，正文与工具调用各自成行。"""
    keys = ["role", *(key for key in message if key != "role")]
    attributes = ", ".join(
        f"{_inline(key)}: {_inline(message[key])}"
        for key in keys
        if key not in _MESSAGE_FIELDS
    )
    return [attributes] + [
        f"{_inline(key)}: {_inline(message[key])}"
        for key in _MESSAGE_FIELDS
        if key in message
    ]


def _inline(value: JsonValue) -> str:
    """将单个 JSON 值编码为保留中文的行内文本。"""
    return json.dumps(value, ensure_ascii=False)
