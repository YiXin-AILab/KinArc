"""evaluation.rules L1 规则库测试。"""

import pytest

from evaluation.models import CaseOutcome, Expected, ToolCallRecord
from evaluation.rules import (
    RULE_REGISTRY,
    auto_rules,
    is_refusal,
    parse_retrieved_doc_ids,
    register_rule,
    run_rules,
    select_rules,
)


def _outcome(**overrides: object) -> CaseOutcome:
    base: dict[str, object] = {"case_id": "c1", "reply": "正常回复"}
    base.update(overrides)
    return CaseOutcome.model_validate(base)


def test_registry_contains_all_builtin_rules() -> None:
    assert set(RULE_REGISTRY) == {
        "retrieval_hit",
        "forbidden_words",
        "refusal_correct",
        "required_tools_called",
        "forbidden_tools_not_called",
        "latency_under",
        "tokens_under",
    }


def test_register_rule_rejects_duplicate() -> None:
    with pytest.raises(ValueError, match="重复注册"):
        register_rule("retrieval_hit")(lambda outcome, expected: None)  # type: ignore[arg-type, return-value]


def test_auto_rules_empty_expected() -> None:
    assert auto_rules(Expected()) == []


def test_auto_rules_full_expected() -> None:
    expected = Expected(
        key_points=["a"],
        forbidden=["x"],
        must_refuse=True,
        gold_doc_ids=["d1"],
        required_tools=["t1"],
        forbidden_tools=["t2"],
        max_latency_seconds=10.0,
        max_total_tokens=100,
    )
    assert auto_rules(expected) == [
        "retrieval_hit",
        "forbidden_words",
        "refusal_correct",
        "required_tools_called",
        "forbidden_tools_not_called",
        "latency_under",
        "tokens_under",
    ]


def test_select_rules_explicit_and_unknown() -> None:
    assert select_rules(["forbidden_words"], None) == ["forbidden_words"]
    with pytest.raises(ValueError, match="未知规则"):
        select_rules(["nope"], None)


def test_retrieval_hit_pass_and_missing() -> None:
    expected = Expected(gold_doc_ids=["d1", "d2"])
    passed = RULE_REGISTRY["retrieval_hit"](
        _outcome(retrieved_doc_ids=["d1", "d2", "d3"]), expected
    )
    assert passed.passed is True
    failed = RULE_REGISTRY["retrieval_hit"](
        _outcome(retrieved_doc_ids=["d1"]), expected
    )
    assert failed.passed is False
    assert "d2" in failed.detail


def test_forbidden_words_boundary() -> None:
    expected = Expected(forbidden=["绝对免费"])
    assert (
        RULE_REGISTRY["forbidden_words"](
            _outcome(reply="价格详询门店"), expected
        ).passed
        is True
    )
    result = RULE_REGISTRY["forbidden_words"](
        _outcome(reply="这是绝对免费的"), expected
    )
    assert result.passed is False
    assert "绝对免费" in result.detail


def test_forbidden_words_ignores_negated_clarification() -> None:
    expected = Expected(forbidden=["终身保修", "全部免费"])
    denial = _outcome(reply="汽车没有“终身保修”，质保期内也不是全部免费。")
    assert RULE_REGISTRY["forbidden_words"](denial, expected).passed is True
    assertion = _outcome(reply="汽车提供终身保修，质保期内全部免费。")
    result = RULE_REGISTRY["forbidden_words"](assertion, expected)
    assert result.passed is False
    assert "终身保修" in result.detail and "全部免费" in result.detail
    mixed = _outcome(reply="没有终身保修，但易损件之外全部免费。")
    mixed_result = RULE_REGISTRY["forbidden_words"](mixed, expected)
    assert mixed_result.passed is False
    assert "全部免费" in mixed_result.detail
    assert "终身保修" not in mixed_result.detail


def test_refusal_correct_both_directions() -> None:
    refusal_reply = _outcome(reply="抱歉，这不在我的服务范围内，建议您咨询专业人士")
    normal_reply = _outcome(reply="A 保价格区间为 1100-1400 元")
    must = Expected(must_refuse=True)
    assert RULE_REGISTRY["refusal_correct"](refusal_reply, must).passed is True
    assert RULE_REGISTRY["refusal_correct"](normal_reply, must).passed is False
    not_must = Expected(must_refuse=False)
    assert RULE_REGISTRY["refusal_correct"](normal_reply, not_must).passed is True
    assert RULE_REGISTRY["refusal_correct"](refusal_reply, not_must).passed is False


def test_is_refusal_keywords() -> None:
    assert is_refusal("这个问题我无法回答") is True
    assert is_refusal("用药方面我不能给您推荐，请就医") is True
    assert is_refusal("股票投资这块我确实帮不上忙") is True
    assert is_refusal("写诗这件事恐怕帮不上您") is True
    assert is_refusal("医药健康不在我的专业范围内") is True
    assert is_refusal("这超出了我作为售后客服的服务范围") is True
    assert is_refusal("A 保 1100 元起") is False
    assert is_refusal("自定义拒绝", keywords=("自定义",)) is True


def test_required_and_forbidden_tools() -> None:
    calls = [
        ToolCallRecord(name="kb_search"),
        ToolCallRecord(name="create_appointment"),
    ]
    outcome = _outcome(tool_calls=calls)
    met = Expected(required_tools=["kb_search", "create_appointment"])
    assert RULE_REGISTRY["required_tools_called"](outcome, met).passed is True
    missing = Expected(required_tools=["escalate_emergency"])
    result = RULE_REGISTRY["required_tools_called"](outcome, missing)
    assert result.passed is False
    assert "escalate_emergency" in result.detail
    forbidden = Expected(forbidden_tools=["kb_search"])
    assert (
        RULE_REGISTRY["forbidden_tools_not_called"](outcome, forbidden).passed is False
    )
    clean = Expected(forbidden_tools=["file_complaint"])
    assert RULE_REGISTRY["forbidden_tools_not_called"](outcome, clean).passed is True


def test_latency_under_boundary() -> None:
    expected = Expected(max_latency_seconds=5.0)
    assert (
        RULE_REGISTRY["latency_under"](_outcome(latency_seconds=5.0), expected).passed
        is True
    )
    assert (
        RULE_REGISTRY["latency_under"](_outcome(latency_seconds=5.1), expected).passed
        is False
    )


def test_tokens_under_and_missing_usage() -> None:
    expected = Expected(max_total_tokens=1000)
    assert (
        RULE_REGISTRY["tokens_under"](_outcome(total_tokens=999), expected).passed
        is True
    )
    assert (
        RULE_REGISTRY["tokens_under"](_outcome(total_tokens=1001), expected).passed
        is False
    )
    missing = RULE_REGISTRY["tokens_under"](_outcome(total_tokens=None), expected)
    assert missing.passed is False
    assert "未透出" in missing.detail


def test_run_rules_short_circuits_on_error_outcome() -> None:
    outcome = _outcome(error="TimeoutError: boom")
    results = run_rules(
        ["retrieval_hit", "forbidden_words"],
        outcome,
        Expected(gold_doc_ids=["d1"], forbidden=["x"]),
    )
    assert len(results) == 2
    assert all(not result.passed for result in results)
    assert all("执行失败" in result.detail for result in results)


def test_parse_retrieved_doc_ids_dedup_and_order() -> None:
    content = (
        "## [pricing-001] 标题一\n正文\n"
        "其他行\n"
        "## [tech-002] 标题二\n"
        "## [pricing-001] 重复\n"
    )
    assert parse_retrieved_doc_ids(content) == ["pricing-001", "tech-002"]
    assert parse_retrieved_doc_ids("没有命中") == []
