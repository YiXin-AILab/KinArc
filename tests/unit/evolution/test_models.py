"""Evolution 通用领域模型测试。"""

import pytest
from evolution.models import (
    CaseAttempt,
    CaseEvaluation,
    CaseExecution,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
    ParentComparison,
)
from pydantic import ValidationError


def _metadata() -> ExecutionMetadata:
    return ExecutionMetadata(total_tokens=3, duration_seconds=0.5)


def _execution(case_id: str) -> CaseExecution:
    return CaseExecution(
        id=case_id,
        result="succeeded",
        tracelog={"events": []},
        metadata=_metadata(),
    )


def _evaluation(case_id: str, score: float = 1.0) -> CaseEvaluation:
    return CaseEvaluation(
        id=case_id,
        result="succeeded",
        tracelog={"events": []},
        metadata=_metadata(),
        expected={"outcome": "done"},
        score=score,
        feedback="ok",
    )


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan"), float("inf")])
def test_evaluation_rejects_scores_outside_normalized_range(score: float) -> None:
    """拒绝会破坏比较和满分停止语义的评分。"""
    with pytest.raises(ValidationError):
        _evaluation("case-1", score)
    with pytest.raises(ValidationError):
        EvaluationResult(total_score=score, cases=())
    payload = _evaluation("case-1").model_dump()
    payload["previous_score"] = score
    with pytest.raises(ValidationError):
        CaseEvaluation.model_validate(payload)


@pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
def test_evaluation_accepts_normalized_scores(score: float) -> None:
    """归一化评分包含两端边界并允许部分得分。"""
    case = _evaluation("case-1", score)
    assert EvaluationResult(total_score=score, cases=(case,)).total_score == score


def test_case_execution_rejects_empty_id() -> None:
    """空 ID 不得进入执行事实。"""
    with pytest.raises(ValidationError):
        _execution("")


def test_case_execution_defaults_to_single_non_retryable_attempt() -> None:
    """未发生 retry 的 Case 不得生成冗余 Attempt 历史。"""
    case = _execution("task_001")

    assert case.retryable is False
    assert case.attempts == ()


def test_case_execution_accepts_consistent_attempt_history() -> None:
    """发生 retry 后顶层必须表达最终结果和累计资源。"""
    attempts = (
        CaseAttempt(
            result="timeout",
            tracelog={"error": "timeout"},
            metadata=ExecutionMetadata(total_tokens=2, duration_seconds=0.2),
        ),
        CaseAttempt(
            result="completed",
            tracelog={"messages": []},
            metadata=ExecutionMetadata(total_tokens=3, duration_seconds=0.3),
        ),
    )

    case = CaseExecution(
        id="task_001",
        result="completed",
        tracelog={"messages": []},
        metadata=ExecutionMetadata(total_tokens=5, duration_seconds=0.5),
        attempts=attempts,
    )

    assert case.attempts == attempts


def test_case_execution_rejects_single_attempt_history() -> None:
    """单次执行必须使用空历史，非空历史至少代表一次重试。"""
    attempt = CaseAttempt(
        result="completed",
        tracelog={"messages": []},
        metadata=_metadata(),
    )

    with pytest.raises(ValidationError, match="至少包含两次"):
        CaseExecution(
            id="task_001",
            result="completed",
            tracelog={"messages": []},
            metadata=_metadata(),
            attempts=(attempt,),
        )


def test_case_execution_rejects_attempt_history_with_wrong_final_result() -> None:
    """历史末项与顶层最终事实不一致时必须拒绝。"""
    attempt = CaseAttempt(
        result="timeout",
        tracelog={"error": "timeout"},
        metadata=_metadata(),
    )

    with pytest.raises(ValidationError, match="最终 Attempt"):
        CaseExecution(
            id="task_001",
            result="completed",
            tracelog={"messages": []},
            metadata=ExecutionMetadata(total_tokens=6, duration_seconds=1),
            attempts=(attempt, attempt),
        )


def test_case_execution_rejects_wrong_aggregate_metadata() -> None:
    """顶层资源消耗必须等于所有 Attempts 之和。"""
    attempt = CaseAttempt(
        result="timeout",
        tracelog={"error": "timeout"},
        metadata=_metadata(),
    )

    with pytest.raises(ValidationError, match="metadata"):
        CaseExecution(
            id="task_001",
            result="timeout",
            tracelog={"error": "timeout"},
            metadata=ExecutionMetadata(total_tokens=4, duration_seconds=0.5),
            attempts=(attempt, attempt),
        )


def _trial(result: str, tokens: int) -> CaseAttempt:
    return CaseAttempt(
        result=result,
        tracelog={"trial": result},
        metadata=ExecutionMetadata(total_tokens=tokens, duration_seconds=0.5),
    )


def test_case_execution_accepts_consistent_trials() -> None:
    """重复采样顶层必须代表首个 Trial 并累计全部资源。"""
    trials = (_trial("completed", 2), _trial("agent_error", 3), _trial("completed", 4))

    case = CaseExecution(
        id="task_001",
        result="completed",
        tracelog={"trial": "completed"},
        metadata=ExecutionMetadata(total_tokens=9, duration_seconds=1.5),
        trials=trials,
    )

    assert case.trials == trials
    assert case.retryable is False
    assert case.attempts == ()


def test_case_execution_rejects_trials_mixed_with_attempts() -> None:
    """重试历史与重复采样语义互斥，不得同时非空。"""
    attempt = _trial("completed", 3)
    trials = (_trial("completed", 3), _trial("completed", 3))

    with pytest.raises(ValidationError, match="互斥"):
        CaseExecution(
            id="task_001",
            result="completed",
            tracelog={"trial": "completed"},
            metadata=ExecutionMetadata(total_tokens=6, duration_seconds=1),
            attempts=(attempt, attempt),
            trials=trials,
        )


def test_case_execution_rejects_single_trial() -> None:
    """单次执行必须使用空 Trials，非空至少代表一次重复采样。"""
    with pytest.raises(ValidationError, match="至少包含两次 Trial"):
        CaseExecution(
            id="task_001",
            result="completed",
            tracelog={"trial": "completed"},
            metadata=_metadata(),
            trials=(_trial("completed", 3),),
        )


def test_case_execution_rejects_trials_with_wrong_representative() -> None:
    """顶层代表事实与首个 Trial 不一致时必须拒绝。"""
    trials = (_trial("completed", 3), _trial("completed", 3))

    with pytest.raises(ValidationError, match="首个 Trial"):
        CaseExecution(
            id="task_001",
            result="agent_error",
            tracelog={"trial": "completed"},
            metadata=ExecutionMetadata(total_tokens=6, duration_seconds=1),
            trials=trials,
        )


def test_case_execution_rejects_wrong_trials_aggregate_metadata() -> None:
    """顶层资源消耗必须等于所有 Trials 之和。"""
    trials = (_trial("completed", 3), _trial("completed", 3))

    with pytest.raises(ValidationError, match="metadata"):
        CaseExecution(
            id="task_001",
            result="completed",
            tracelog={"trial": "completed"},
            metadata=ExecutionMetadata(total_tokens=7, duration_seconds=1),
            trials=trials,
        )


def test_execution_result_rejects_duplicate_case_ids() -> None:
    """重复执行 ID 会令单 Case 事实不可定位。"""
    case = _execution("task_001")

    with pytest.raises(ValidationError, match="重复"):
        ExecutionResult(cases=(case, case))


def test_evaluation_result_rejects_duplicate_case_ids() -> None:
    """重复评分 ID 会使 Dataset 聚合结果含糊。"""
    case = _evaluation("task_001")

    with pytest.raises(ValidationError, match="重复"):
        EvaluationResult(total_score=1.0, cases=(case, case))


def test_evaluation_with_comparison_returns_new_frozen_fact() -> None:
    """加入父子 Train 比较时必须保留原评分事实不变。"""
    evaluation = EvaluationResult(
        total_score=1.0,
        cases=(_evaluation("task_001"),),
    )
    comparison = ParentComparison(
        previous_candidate_id="candidate-a",
        current_candidate_id="candidate-b",
        previous_total_score=0.5,
        current_total_score=1.0,
        total_score_delta=0.5,
        previous_case_count=1,
        current_case_count=1,
        improved_case_count=1,
        regressed_case_count=0,
        unchanged_case_count=0,
    )

    updated = evaluation.with_comparison(comparison)

    assert evaluation.parent_comparison is None
    assert updated.parent_comparison == comparison
