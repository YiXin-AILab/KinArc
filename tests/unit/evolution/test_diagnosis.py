"""确定性诊断简报测试。"""

import pytest
from evolution.diagnosis import (
    CaseSeverity,
    ProblemLibrary,
    build_diagnosis_brief,
    render_brief_markdown,
    render_deferred_section,
    split_actionable_clusters,
    update_problem_library,
)
from evolution.models import (
    CaseEvaluation,
    EvaluationResult,
    ExecutionMetadata,
    ParentComparison,
)

_METADATA = ExecutionMetadata(total_tokens=1, duration_seconds=0.1)


@pytest.mark.parametrize("has_actionable", [False, True])
def test_render_brief_counts_deferred_failures(has_actionable: bool) -> None:
    """失败总数包含暂缓簇，不把无可处理簇误报为全部通过。"""
    cases: tuple[CaseEvaluation, ...] = (
        _case("A", 0, failure_kind="refund_missing"),
        _case("D", 1),
    )
    if has_actionable:
        cases += (
            _case("B", 0, failure_kind="refund_missing"),
            _case("C", 0, failure_kind="tracking_wrong"),
        )
    brief = build_diagnosis_brief("candidate-a", _evaluation(cases))
    library = update_problem_library(ProblemLibrary(), brief)
    actionable, deferred = split_actionable_clusters(brief, library)

    text = render_brief_markdown(actionable, deferred_clusters=deferred)

    assert f"未通过 Case {3 if has_actionable else 1} 个" in text
    assert f"本轮可处理：{2 if has_actionable else 0} 个 Case" in text
    assert "本轮暂缓：1 个 Case / 1 个簇" in text
    assert "全部通过" not in text
    if not has_actionable:
        assert "本轮无可处理失败簇" in text


def _case(
    case_id: str,
    score: float,
    *,
    previous_score: float | None = None,
    feedback: str = "",
    failure_kind: str | None = None,
) -> CaseEvaluation:
    return CaseEvaluation(
        id=case_id,
        result="result",
        tracelog={},
        metadata=_METADATA,
        expected={},
        score=score,
        previous_score=previous_score,
        feedback=feedback,
        failure_kind=failure_kind,
    )


def _evaluation(
    cases: tuple[CaseEvaluation, ...],
    *,
    total_score: float = 0.0,
    comparison: ParentComparison | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        total_score=total_score,
        cases=cases,
        parent_comparison=comparison,
    )


def test_same_failure_text_despite_case_specific_details_clusters_together() -> None:
    """含 Case ID、数字、路径和引号内容的反馈归一化后落入同一签名。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case(
                    "task_001",
                    0,
                    feedback='Error: expected "bal-1" in data/db.json for task_001',
                ),
                _case(
                    "task_002",
                    0,
                    feedback='Error: expected "bal-2" in data/db.json for task_002',
                ),
            )
        ),
    )
    assert len(brief.clusters) == 1
    cluster = brief.clusters[0]
    assert tuple(case.id for case in cluster.cases) == ("task_001", "task_002")
    assert "bal" not in cluster.signature
    assert "001" not in cluster.signature
    assert "/" not in cluster.signature


def test_empty_feedback_falls_back_to_unclassified_signature() -> None:
    """空反馈与无失败指示词的反馈都回落到固定签名，且不互相合并。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("case-a", 0, feedback=""),
                _case("case-b", 0, feedback="   \n  \n"),
            )
        ),
    )
    assert len(brief.clusters) == 2
    assert {cluster.signature for cluster in brief.clusters} == {"unclassified_failure"}
    assert all(len(cluster.cases) == 1 for cluster in brief.clusters)


def test_failure_kind_takes_precedence_over_feedback_heuristic() -> None:
    """Evaluator 提供的 Case 不变失败类别直接作为签名，忽略 feedback 文本。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case(
                    "case-a",
                    0,
                    feedback='{"action_checks": [{"action_match": false}]}',
                    failure_kind="action_mismatch:apply_for_credit_card",
                ),
                _case(
                    "case-b",
                    0,
                    feedback="completely different feedback text",
                    failure_kind="action_mismatch:apply_for_credit_card",
                ),
            )
        ),
    )
    assert len(brief.clusters) == 1
    cluster = brief.clusters[0]
    assert cluster.signature == "action_mismatch:apply_for_credit_card"
    assert tuple(case.id for case in cluster.cases) == ("case-a", "case-b")


def test_low_quality_signature_falls_back_to_unclassified() -> None:
    """只剩标量残片或过短的归一化签名按无法归类处理。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("case-a", 0, feedback='{"a": null, "b": true, "c": false}'),
                _case("case-b", 0, feedback="err"),
            )
        ),
    )
    assert len(brief.clusters) == 2
    assert {cluster.signature for cluster in brief.clusters} == {"unclassified_failure"}


def test_unclassified_clusters_stay_actionable_and_out_of_library() -> None:
    """unclassified 簇不延后、不入库，始终交给 Improver 逐个分流。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation((_case("case-a", 0, feedback=""),)),
    )
    library = update_problem_library(ProblemLibrary(), brief)
    assert library.entries == ()
    actionable, deferred = split_actionable_clusters(brief, library)
    assert deferred == ()
    assert len(actionable.clusters) == 1
    assert actionable.clusters[0].signature == "unclassified_failure"


def test_cluster_severity_is_most_severe_member_and_ordering_is_stable() -> None:
    """簇严重度取簇内最高，排序按严重度、簇大小、签名稳定。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("case-a", 0, feedback="timeout while calling tool"),
                _case("case-b", 0.5, feedback="timeout while calling tool"),
                _case(
                    "case-c",
                    0,
                    previous_score=1.0,
                    feedback="missing required artifact",
                ),
                _case("case-d", 0, feedback="agent timeout exceeded"),
                _case("case-e", 0, feedback="another timeout happened"),
            ),
            total_score=0.1,
        ),
    )
    assert [cluster.cluster_id for cluster in brief.clusters] == [
        "cluster_1",
        "cluster_2",
        "cluster_3",
        "cluster_4",
    ]
    regression, shared, agent_timeout, another_timeout = brief.clusters
    assert regression.severity is CaseSeverity.REGRESSION
    assert regression.signature.startswith("missing_required_artifact")
    assert shared.severity is CaseSeverity.TOTAL_FAILURE
    assert len(shared.cases) == 2
    assert {case.id for case in shared.cases} == {"case-a", "case-b"}
    assert agent_timeout.severity is CaseSeverity.TOTAL_FAILURE
    assert agent_timeout.signature == "agent_timeout_exceeded"
    assert another_timeout.signature == "another_timeout_happened"
    repeat = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("case-a", 0, feedback="timeout while calling tool"),
                _case("case-b", 0.5, feedback="timeout while calling tool"),
                _case(
                    "case-c",
                    0,
                    previous_score=1.0,
                    feedback="missing required artifact",
                ),
                _case("case-d", 0, feedback="agent timeout exceeded"),
                _case("case-e", 0, feedback="another timeout happened"),
            ),
            total_score=0.1,
        ),
    )
    assert repeat == brief


def test_regression_requires_crossing_pass_threshold() -> None:
    """severity 只标记跨通过线的退步；任何分数下降都进退步清单。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("case-a", 0.5, previous_score=1.0, feedback="expected mismatch"),
                _case("case-b", 0.0, previous_score=0.5, feedback="expected mismatch"),
                _case("case-c", 1.0, previous_score=1.0, feedback=""),
                _case("case-d", 0.5, previous_score=0.5, feedback="expected mismatch"),
            ),
            total_score=0.5,
        ),
    )
    by_id = {case.id: case for cluster in brief.clusters for case in cluster.cases}
    assert by_id["case-a"].severity is CaseSeverity.REGRESSION
    assert by_id["case-b"].severity is CaseSeverity.TOTAL_FAILURE
    assert by_id["case-d"].severity is CaseSeverity.PARTIAL
    assert tuple(case.id for case in brief.regressed_cases) == ("case-a", "case-b")
    assert brief.passing_case_ids == ("case-c",)


def test_missing_previous_score_never_marks_regression() -> None:
    """没有历史分数的 Case 只按当前分数分层。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation((_case("case-a", 0, feedback="error occurred"),)),
    )
    assert brief.clusters[0].severity is CaseSeverity.TOTAL_FAILURE
    assert brief.regressed_cases == ()


def test_all_passing_produces_empty_clusters() -> None:
    """全部通过时没有失败簇与退步，通过名单包含全部 Case。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (_case("case-a", 1.0), _case("case-b", 1.0)),
            total_score=1.0,
        ),
    )
    assert brief.clusters == ()
    assert brief.regressed_cases == ()
    assert brief.passing_case_ids == ("case-a", "case-b")


def test_empty_evaluation_produces_empty_brief() -> None:
    """空 Evaluation 产出全空简报且可渲染。"""
    brief = build_diagnosis_brief("candidate-a", _evaluation(()))
    assert brief.clusters == ()
    assert brief.passing_case_ids == ()
    text = render_brief_markdown(brief)
    assert "无（全部通过）" in text


def test_brief_reads_baseline_from_parent_comparison() -> None:
    """总分基线来自挑战比较，缺省时为 None。"""
    comparison = ParentComparison(
        previous_candidate_id="candidate-parent",
        current_candidate_id="candidate-a",
        previous_total_score=0.5,
        current_total_score=0.75,
        total_score_delta=0.25,
        previous_case_count=1,
        current_case_count=1,
    )
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation((_case("case-a", 1.0),), total_score=0.75, comparison=comparison),
    )
    assert brief.baseline_candidate_id == "candidate-parent"
    assert brief.previous_total_score == 0.5
    without = build_diagnosis_brief(
        "candidate-a", _evaluation((_case("case-a", 1.0),), total_score=0.75)
    )
    assert without.baseline_candidate_id is None
    assert without.previous_total_score is None


def test_render_contains_fixed_sections_and_details() -> None:
    """渲染输出包含四个固定小节及簇、退步、保护名单细节。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("task_001", 0, previous_score=1.0, feedback="missing artifact"),
                _case("task_002", 0, feedback="timeout"),
                _case("task_003", 1.0),
            ),
            total_score=1 / 3,
        ),
    )
    text = render_brief_markdown(brief)
    assert "## 诊断概要" in text
    assert "## 失败簇" in text
    assert "## 本轮退步 Case" in text
    assert "## 当前通过 Case（回归保护对象）" in text
    assert "cluster_1" in text
    assert "task_001" in text
    assert "task_003" in text
    assert "1.0 → 0.0" not in text  # case 分数用 .4g 渲染
    assert "1 → 0" in text


def test_update_problem_library_accumulates_distinct_cases() -> None:
    """单 case 簇按签名入库；不同 case 推进计数，同一 case 重复不推进。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation((_case("case-a", 0, feedback="timeout while calling tool"),)),
    )
    library = update_problem_library(ProblemLibrary(), brief)
    assert len(library.entries) == 1
    entry = library.entries[0]
    assert entry.signature == brief.clusters[0].signature
    assert entry.case_ids == ("case-a",)
    assert entry.status == "accumulating"
    assert entry.severity is CaseSeverity.TOTAL_FAILURE
    assert entry.first_seen_candidate_id == "candidate-a"
    assert entry.last_seen_candidate_id == "candidate-a"

    repeat = update_problem_library(
        library,
        build_diagnosis_brief(
            "candidate-b",
            _evaluation((_case("case-a", 0, feedback="timeout while calling tool"),)),
        ),
    )
    assert repeat.entries[0].case_ids == ("case-a",)
    assert repeat.entries[0].status == "accumulating"
    assert repeat.entries[0].last_seen_candidate_id == "candidate-b"

    matured = update_problem_library(
        library,
        build_diagnosis_brief(
            "candidate-c",
            _evaluation((_case("case-b", 0.5, feedback="timeout while calling tool"),)),
        ),
    )
    assert matured.entries[0].case_ids == ("case-a", "case-b")
    assert matured.entries[0].status == "ready"
    assert matured.entries[0].severity is CaseSeverity.PARTIAL
    assert matured.entries[0].first_seen_candidate_id == "candidate-a"
    assert matured.entries[0].last_seen_candidate_id == "candidate-c"


def test_update_problem_library_marks_disappeared_signatures_resolved() -> None:
    """本轮不再失败的签名置为 resolved，重新出现时恢复并按计数定状态。"""
    first = build_diagnosis_brief(
        "candidate-a",
        _evaluation((_case("case-a", 0, feedback="timeout while calling tool"),)),
    )
    library = update_problem_library(ProblemLibrary(), first)
    resolved = update_problem_library(
        library,
        build_diagnosis_brief("candidate-b", _evaluation((_case("case-a", 1.0),))),
    )
    assert resolved.entries[0].status == "resolved"
    assert resolved.entries[0].last_seen_candidate_id == "candidate-a"

    revived = update_problem_library(resolved, first)
    assert revived.entries[0].status == "accumulating"
    assert revived.entries[0].last_seen_candidate_id == "candidate-a"


def test_update_problem_library_skips_regression_and_multi_case_clusters() -> None:
    """regression 与多 case 簇不入库，但已有条目不因仍在失败而误判 resolved。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("case-a", 0, previous_score=1.0, feedback="missing artifact"),
                _case("case-b", 0, feedback="timeout"),
                _case("case-c", 0, feedback="timeout"),
            )
        ),
    )
    library = update_problem_library(ProblemLibrary(), brief)
    assert library.entries == ()

    seed_library = update_problem_library(
        ProblemLibrary(),
        build_diagnosis_brief(
            "candidate-x",
            _evaluation((_case("case-z", 0, feedback="timeout"),)),
        ),
    )
    updated = update_problem_library(seed_library, brief)
    entry = updated.entries[0]
    assert entry.case_ids == ("case-z",)
    assert entry.status == "accumulating"
    assert entry.severity is CaseSeverity.TOTAL_FAILURE
    assert entry.last_seen_candidate_id == "candidate-a"


def test_split_actionable_clusters_defers_only_immature_single_cases() -> None:
    """单 case 未成熟簇延后；regression、多 case 与已成熟簇始终可处理。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation(
            (
                _case("case-a", 0, feedback="timeout while calling tool"),
                _case("case-b", 0, previous_score=1.0, feedback="missing artifact"),
                _case("case-c", 0, feedback="denied by policy"),
                _case("case-d", 0, feedback="denied by policy"),
            )
        ),
    )
    library = update_problem_library(ProblemLibrary(), brief)
    actionable, deferred = split_actionable_clusters(brief, library)
    by_signature = {cluster.signature: cluster for cluster in brief.clusters}
    timeout = by_signature["timeout_while_calling_tool"]
    regression = by_signature["missing_artifact"]
    denied = by_signature["denied_by_policy"]
    assert actionable.clusters == (regression, denied)
    assert deferred == (timeout,)
    assert actionable.evidence_candidate_id == brief.evidence_candidate_id
    assert actionable.regressed_cases == brief.regressed_cases

    matured = update_problem_library(
        library,
        build_diagnosis_brief(
            "candidate-b",
            _evaluation((_case("case-e", 0, feedback="timeout while calling tool"),)),
        ),
    )
    repeat_brief = build_diagnosis_brief(
        "candidate-b",
        _evaluation((_case("case-e", 0, feedback="timeout while calling tool"),)),
    )
    actionable, deferred = split_actionable_clusters(repeat_brief, matured)
    assert deferred == ()
    assert len(actionable.clusters) == 1
    assert actionable.clusters[0].signature == "timeout_while_calling_tool"


def test_split_respects_custom_min_cases() -> None:
    """阈值可配置：达到 min_cases 个不同 case 前保持延后。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation((_case("case-a", 0, feedback="timeout"),)),
    )
    library = update_problem_library(ProblemLibrary(), brief, min_cases=1)
    actionable, deferred = split_actionable_clusters(brief, library, min_cases=1)
    assert deferred == ()
    assert len(actionable.clusters) == 1


def test_render_deferred_section_lists_progress() -> None:
    """延后小节逐条列出签名、severity、积累进度与最近出现版本。"""
    brief = build_diagnosis_brief(
        "candidate-a",
        _evaluation((_case("case-a", 0.5, feedback="timeout while calling tool"),)),
    )
    library = update_problem_library(ProblemLibrary(), brief)
    actionable, deferred = split_actionable_clusters(brief, library)
    assert actionable.clusters == ()
    text = render_deferred_section(deferred, library)
    assert text.startswith("## 问题库（单 case 簇积累中，本轮不处理）")
    assert "- 签名 `timeout_while_calling_tool` · partial · 已积累 1/2 个 case" in text
    assert "最近出现于 candidate-a" in text
    assert render_deferred_section((), library).endswith("\n无")


def test_render_brief_annotates_ready_clusters() -> None:
    """已成熟签名的可处理簇在标题后附问题库积累说明。"""
    library = update_problem_library(
        ProblemLibrary(),
        build_diagnosis_brief(
            "candidate-a",
            _evaluation((_case("case-a", 0, feedback="timeout while calling tool"),)),
        ),
    )
    library = update_problem_library(
        library,
        build_diagnosis_brief(
            "candidate-b",
            _evaluation((_case("case-b", 0, feedback="timeout while calling tool"),)),
        ),
    )
    brief = build_diagnosis_brief(
        "candidate-c",
        _evaluation(
            (
                _case("case-b", 0, feedback="timeout while calling tool"),
                _case("case-c", 0, feedback="denied by policy"),
            )
        ),
    )
    text = render_brief_markdown(brief, problem_library=library)
    assert "签名 `timeout_while_calling_tool`（问题库已积累 2 个 case）" in text
    assert "签名 `denied_by_policy`（问题库已积累" not in text
    assert "（问题库已积累" not in render_brief_markdown(brief)
