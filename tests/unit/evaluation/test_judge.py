"""evaluation.judge 多维裁判测试（mock litellm，不访问真实 LLM）。"""

import json
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from evaluation.judge import (
    DIMENSION_REGISTRY,
    LlmJudge,
    extract_json_object,
    select_dimensions,
)
from evaluation.models import CaseOutcome, EvalCase, Expected, JudgeLlmConfig


def _config() -> JudgeLlmConfig:
    return JudgeLlmConfig(
        model="openai/test-model",
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",
        timeout_seconds=5.0,
    )


def _case(**overrides: Any) -> EvalCase:
    base: dict[str, Any] = {"id": "c1", "input": "A 保多少钱？"}
    base.update(overrides)
    return EvalCase.model_validate(base)


def _outcome(**overrides: Any) -> CaseOutcome:
    base: dict[str, Any] = {"case_id": "c1", "reply": "A 保 1100-1400 元。"}
    base.update(overrides)
    return CaseOutcome.model_validate(base)


def _fake_response(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
            )
        ]
    )


def _patch_acompletion(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    monkeypatch.setattr(litellm, "acompletion", handler)


def test_select_dimensions_defaults_skip_accuracy_without_key_points() -> None:
    names = [spec.name for spec in select_dimensions(_case())]
    assert names == ["faithfulness", "completeness", "compliance", "human_likeness"]
    with_points = _case(expected=Expected(key_points=["价格区间"]))
    names = [spec.name for spec in select_dimensions(with_points)]
    assert names == [
        "faithfulness",
        "accuracy",
        "completeness",
        "compliance",
        "human_likeness",
    ]


def test_select_dimensions_explicit_and_unknown() -> None:
    specs = select_dimensions(_case(dims=["compliance"]))
    assert [spec.name for spec in specs] == ["compliance"]
    with pytest.raises(ValueError, match="未知"):
        select_dimensions(_case(dims=["nope"]))


def test_extract_json_object_tolerates_wrapping_text() -> None:
    assert extract_json_object('前言 {"score": 0.5, "rationale": "ok"} 后记') == {
        "score": 0.5,
        "rationale": "ok",
    }
    with pytest.raises(ValueError):
        extract_json_object("没有 JSON")


@pytest.mark.asyncio
async def test_score_case_success_all_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        prompt = kwargs["messages"][0]["content"]
        calls.append(prompt)
        assert kwargs["api_key"] == "test-key"
        return _fake_response({"score": 0.9, "rationale": "好"})

    _patch_acompletion(monkeypatch, fake_acompletion)
    judge = LlmJudge(_config(), concurrency=2)
    case = _case(expected=Expected(key_points=["价格"]))
    dims = await judge.score_case(case, _outcome())
    assert len(calls) == 5
    assert {dim.dimension for dim in dims} == set(DIMENSION_REGISTRY)
    assert all(dim.score == 0.9 for dim in dims)


@pytest.mark.asyncio
async def test_faithfulness_prompt_includes_reference_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        prompts.append(kwargs["messages"][0]["content"])
        return _fake_response({"score": 1.0, "rationale": "ok"})

    _patch_acompletion(monkeypatch, fake_acompletion)
    judge = LlmJudge(_config())
    grounded = _outcome(tool_outputs=["## [pricing-001] A 保 1100-1400 元"])
    await judge.score_case(_case(dims=["faithfulness"]), grounded)
    assert "## [pricing-001] A 保 1100-1400 元" in prompts[0]
    prompts.clear()
    await judge.score_case(_case(dims=["faithfulness"]), _outcome())
    assert "未调用任何检索或业务工具" in prompts[0]


@pytest.mark.asyncio
async def test_score_case_retries_once_then_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def failing(**kwargs: Any) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("gateway boom")

    _patch_acompletion(monkeypatch, failing)
    judge = LlmJudge(_config(), concurrency=1)
    dims = await judge.score_case(_case(dims=["faithfulness"]), _outcome())
    assert attempts == 2
    assert dims[0].score is None
    assert dims[0].error is not None
    assert "RuntimeError" in dims[0].error


@pytest.mark.asyncio
async def test_score_case_retry_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def flaky(**kwargs: Any) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return _fake_response({"score": 1.0, "rationale": "恢复"})

    _patch_acompletion(monkeypatch, flaky)
    judge = LlmJudge(_config())
    dims = await judge.score_case(_case(dims=["compliance"]), _outcome())
    assert attempts == 2
    assert dims[0].score == 1.0
    assert dims[0].error is None


@pytest.mark.asyncio
async def test_score_case_invalid_json_and_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def bad_json(**kwargs: Any) -> SimpleNamespace:
        return _fake_response({"score": 2.0, "rationale": "越界"})

    _patch_acompletion(monkeypatch, bad_json)
    judge = LlmJudge(_config())
    dims = await judge.score_case(_case(dims=["compliance"]), _outcome())
    assert dims[0].score is None
    assert dims[0].error is not None


@pytest.mark.asyncio
async def test_score_case_skips_error_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_run(**kwargs: Any) -> SimpleNamespace:
        raise AssertionError("执行失败的 Case 不应调用裁判")

    _patch_acompletion(monkeypatch, should_not_run)
    judge = LlmJudge(_config())
    dims = await judge.score_case(_case(), _outcome(error="boom"))
    assert dims == []


@pytest.mark.asyncio
async def test_score_cases_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**kwargs: Any) -> SimpleNamespace:
        return _fake_response({"score": 0.5, "rationale": "ok"})

    _patch_acompletion(monkeypatch, fake)
    judge = LlmJudge(_config())
    items = [
        (_case(id="a"), _outcome(case_id="a")),
        (_case(id="b"), _outcome(case_id="b")),
    ]
    results = await judge.score_cases(items)
    assert len(results) == 2
