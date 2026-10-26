"""平台知识库批测指标与 diff 纯函数测试。"""

from server.platform.knowledge.routes import (
    PlatformKbAnswerDiff,
    PlatformKbAnswerMetrics,
    PlatformKbBatchItem,
    PlatformKbDocMetrics,
    PlatformKbMissingDoc,
    compute_doc_metrics,
    summarize_batch_results,
    token_overlap_diff,
)


def test_doc_metrics_full_recall() -> None:
    """全部期望文档命中最终结果：Top1/Top3/集合召回全部命中。"""
    metrics = compute_doc_metrics(
        expected_doc_ids=("a", "b"),
        surviving_doc_ids=("a", "b"),
        candidate_doc_ids=("a", "b"),
        full_ranking=None,
    )

    assert metrics.top1_hit is True
    assert metrics.top3_hit is True
    assert metrics.set_recall is True
    assert metrics.gate_false_kills == ()
    assert metrics.missing == ()


def test_doc_metrics_top1_miss_but_top3_hit() -> None:
    """期望文档在第二/三名：Top1 未命中、Top3 命中、集合召回成立。"""
    metrics = compute_doc_metrics(
        expected_doc_ids=("b",),
        surviving_doc_ids=("x", "b"),
        candidate_doc_ids=("x", "b"),
        full_ranking=None,
    )

    assert metrics.top1_hit is False
    assert metrics.top3_hit is True
    assert metrics.set_recall is True


def test_doc_metrics_gate_false_kill() -> None:
    """期望文档检索命中但被裁判剔除：计误杀并标注 gate_filtered 排名。"""
    metrics = compute_doc_metrics(
        expected_doc_ids=("a",),
        surviving_doc_ids=("b",),
        candidate_doc_ids=("a", "b"),
        full_ranking=None,
    )

    assert metrics.set_recall is False
    assert metrics.gate_false_kills == ("a",)
    assert metrics.missing == (
        PlatformKbMissingDoc(doc_id="a", rank=1, gate_filtered=True),
    )


def test_doc_metrics_missing_reports_full_corpus_rank() -> None:
    """未进入候选的期望文档给出无截断全库排名；不在库中 rank=None。"""
    metrics = compute_doc_metrics(
        expected_doc_ids=("c", "ghost"),
        surviving_doc_ids=("a",),
        candidate_doc_ids=("a",),
        full_ranking=("a", "b", "c"),
    )

    assert metrics.gate_false_kills == ()
    assert metrics.missing == (
        PlatformKbMissingDoc(doc_id="c", rank=3, gate_filtered=False),
        PlatformKbMissingDoc(doc_id="ghost", rank=None, gate_filtered=False),
    )


def test_doc_metrics_empty_survivors_no_top_hit() -> None:
    """零命中时 Top1/Top3 均为未命中而不是异常。"""
    metrics = compute_doc_metrics(
        expected_doc_ids=("a",),
        surviving_doc_ids=(),
        candidate_doc_ids=(),
        full_ranking=("b", "a"),
    )

    assert metrics.top1_hit is False
    assert metrics.top3_hit is False
    assert metrics.missing[0].rank == 2


def test_token_overlap_diff_partitions_tokens() -> None:
    """matched/missing/extra 三段覆盖参考与内容的词级重叠。"""
    diff = token_overlap_diff("工时费按小时收取", "工时费标准为 100 元")

    assert "时费" in diff.matched
    assert "收取" in diff.missing
    assert "100" in diff.extra
    assert set(diff.matched).isdisjoint(diff.missing)
    assert set(diff.matched).isdisjoint(diff.extra)


def _answer_item(
    coverage: float | None, error: str | None = None
) -> PlatformKbBatchItem:
    return PlatformKbBatchItem(
        question="q",
        chunks=(),
        no_relevant_docs=False,
        doc_metrics=None,
        answer_metrics=PlatformKbAnswerMetrics(
            coverage=coverage,
            rationale="",
            diff=PlatformKbAnswerDiff(matched=(), missing=(), extra=()),
            diff_note="",
            error=None if coverage is not None else "裁判评分失败（RuntimeError）",
        ),
        error=error,
    )


def _doc_item(
    top1: bool, recall: bool, kills: tuple[str, ...] = ()
) -> PlatformKbBatchItem:
    return PlatformKbBatchItem(
        question="q",
        chunks=(),
        no_relevant_docs=False,
        doc_metrics=PlatformKbDocMetrics(
            expected_doc_ids=("a",),
            top1_hit=top1,
            top3_hit=top1,
            set_recall=recall,
            gate_false_kills=kills,
            missing=(),
        ),
        answer_metrics=None,
        error=None,
    )


def test_summarize_splits_doc_and_answer_questions() -> None:
    """两类题分开统计；失败题不计入；覆盖率只平均裁判成功的题。"""
    items = (
        _doc_item(top1=True, recall=True),
        _doc_item(top1=False, recall=False, kills=("a",)),
        _answer_item(0.8),
        _answer_item(None),
        PlatformKbBatchItem(
            question="boom",
            chunks=(),
            no_relevant_docs=True,
            doc_metrics=None,
            answer_metrics=None,
            error="题目执行失败（RuntimeError）",
        ),
    )

    summary = summarize_batch_results(items)

    assert summary.doc_questions.count == 2
    assert summary.doc_questions.top1_rate == 0.5
    assert summary.doc_questions.set_recall_rate == 0.5
    assert summary.doc_questions.gate_false_kill_count == 1
    assert summary.answer_questions.count == 2
    assert summary.answer_questions.judged_count == 1
    assert summary.answer_questions.average_coverage == 0.8


def test_summarize_empty_batch() -> None:
    """空批次的比率为 0、覆盖率为 None。"""
    summary = summarize_batch_results(())

    assert summary.doc_questions.count == 0
    assert summary.doc_questions.top1_rate == 0.0
    assert summary.answer_questions.average_coverage is None
