"""相关性裁判的解析、重试与降级测试。"""

from typing import Any

import pytest
from agentsystem.knowledge.documents import KnowledgeDocument
from agentsystem.knowledge.gate import RelevanceGate, _parse_verdict
from agentsystem.knowledge.models import RelevanceGateConfig, RelevanceLevel


def _document(doc_id: str) -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_id=doc_id,
        title=f"标题 {doc_id}",
        category=None,
        content=f"内容 {doc_id}",
        updated_at=None,
    )


def _config() -> RelevanceGateConfig:
    return RelevanceGateConfig.model_validate({"model": "openai/small"})


def _completion_payload(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": text}}]}


async def test_judge_returns_verdicts_in_input_order() -> None:
    """并行裁判保持与候选一致的返回顺序。"""
    verdicts_by_doc = {
        "a": '{"relevance": "relevant", "reason": "直接回答"}',
        "b": '{"relevance": "irrelevant", "reason": "无关"}',
    }

    async def fake_completion(**kwargs: Any) -> dict[str, object]:
        messages = kwargs["messages"]
        user = messages[1]["content"]
        doc_id = "a" if "内容 a" in user else "b"
        return _completion_payload(verdicts_by_doc[doc_id])

    gate = RelevanceGate(_config(), completion=fake_completion)

    verdicts = await gate.judge("工时费", [_document("a"), _document("b")])

    assert [verdict.doc_id for verdict in verdicts] == ["a", "b"]
    assert verdicts[0].relevance is RelevanceLevel.RELEVANT
    assert verdicts[0].reason == "直接回答"
    assert verdicts[1].relevance is RelevanceLevel.IRRELEVANT
    assert all(not verdict.gate_error for verdict in verdicts)


async def test_parse_failure_retries_once_then_succeeds() -> None:
    """首次输出非 JSON 时重试一次并采用第二次结果。"""
    calls = 0

    async def flaky_completion(**kwargs: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _completion_payload("我无法判断")
        return _completion_payload('{"relevance": "partial", "reason": "部分相关"}')

    gate = RelevanceGate(_config(), completion=flaky_completion)

    verdicts = await gate.judge("工时费", [_document("a")])

    assert calls == 2
    assert verdicts[0].relevance is RelevanceLevel.PARTIAL
    assert verdicts[0].gate_error is False


async def test_persistent_failure_degrades_to_partial_with_gate_error() -> None:
    """解析持续失败与调用异常都降级为 partial 且不炸主流程。"""

    async def garbage_completion(**kwargs: Any) -> dict[str, object]:
        return _completion_payload("不是 JSON")

    async def failing_completion(**kwargs: Any) -> dict[str, object]:
        raise RuntimeError("provider down")

    garbage_gate = RelevanceGate(_config(), completion=garbage_completion)
    failing_gate = RelevanceGate(_config(), completion=failing_completion)

    garbage = await garbage_gate.judge("工时费", [_document("a")])
    failed = await failing_gate.judge("工时费", [_document("a")])

    for verdict in (*garbage, *failed):
        assert verdict.relevance is RelevanceLevel.PARTIAL
        assert verdict.gate_error is True


def test_parse_verdict_accepts_json_embedded_in_text() -> None:
    """裁判输出允许携带围栏等额外文本，只提取 JSON 对象。"""
    text = '```json\n{"relevance": "relevant", "reason": "命中"}\n```'

    relevance, reason = _parse_verdict(text)

    assert relevance is RelevanceLevel.RELEVANT
    assert reason == "命中"


def test_parse_verdict_rejects_invalid_payload() -> None:
    """缺 JSON、非法 relevance 取值都触发 ValueError 重试路径。"""
    with pytest.raises(ValueError):
        _parse_verdict("没有 JSON")
    with pytest.raises(ValueError):
        _parse_verdict('{"relevance": "unknown", "reason": ""}')
