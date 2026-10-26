"""问题集生成管线、去重、类型分布与沉淀映射的纯函数测试。"""

import hashlib
import json
import re
from pathlib import Path

import pytest
from agentsystem.common.errors import ServiceError
from agentsystem.knowledge.documents import KnowledgeDocument
from evaluation.runner import load_dataset
from server.platform.knowledge.generation import (
    append_eval_dataset,
    dedup_items,
    generate_question_set,
    split_sentences,
    to_eval_cases,
    type_targets,
)
from server.platform.knowledge.questions import QuestionSetItem

_DOCS = tuple(
    KnowledgeDocument(
        doc_id=doc_id,
        title=title,
        category=None,
        content=f"{title}的详细内容",
        updated_at=None,
    )
    for doc_id, title in (
        ("fee", "工时费标准"),
        ("account", "开户流程"),
        ("promo", "促销活动"),
    )
)


def test_type_targets_follow_default_distribution() -> None:
    """每类型保底 1 条，其余按 50/25/15/10 分布，取整余数归 single_doc。"""
    assert type_targets(10) == {
        "single_doc": 6,
        "multi_doc": 2,
        "boundary": 1,
        "out_of_scope": 1,
    }
    assert type_targets(4) == {
        "single_doc": 1,
        "multi_doc": 1,
        "boundary": 1,
        "out_of_scope": 1,
    }
    assert type_targets(8) == {
        "single_doc": 4,
        "multi_doc": 2,
        "boundary": 1,
        "out_of_scope": 1,
    }
    assert type_targets(3) == {
        "single_doc": 3,
        "multi_doc": 0,
        "boundary": 0,
        "out_of_scope": 0,
    }


def test_dedup_items_drops_exact_and_similar_questions() -> None:
    """完全相同与高相似度问题被丢弃，保留顺序稳定。"""
    items = [
        QuestionSetItem(question="S4 A 保多少钱"),
        QuestionSetItem(question="S4 A 保多少钱"),
        QuestionSetItem(question="S4 A 保多少钱啊"),
        QuestionSetItem(question="开户需要什么材料"),
    ]

    kept, dropped = dedup_items(items)

    assert [item.question for item in kept] == ["S4 A 保多少钱", "开户需要什么材料"]
    assert dropped == 2


def test_split_sentences_splits_chinese_punctuation() -> None:
    """按中文句读拆分参考答案为要点列表。"""
    assert split_sentences("价格 100 元。含工时费；以到店报价为准！") == [
        "价格 100 元",
        "含工时费",
        "以到店报价为准",
    ]


def _fake_complete_factory(
    *,
    supplement_returns: str = "[]",
) -> tuple[list[str], object]:
    """按 Prompt 类型分发固定 JSON 输出的 fake LLM；返回 (prompts, complete)。"""

    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if "再生成" in prompt:
            return supplement_returns
        if "库外问题" in prompt:
            return json.dumps([{"question": "这车能帮我订外卖吗"}], ensure_ascii=False)
        if "边界混淆" in prompt:
            return json.dumps(
                [
                    {
                        "question": "工时费 200 元一小时够吗",
                        "expected_doc_ids": ["fee"],
                        "forbidden": ["200 元/小时"],
                        "reference_answer": "工时费按标准收取",
                    }
                ],
                ensure_ascii=False,
            )
        if "组合问题" in prompt:
            return json.dumps(
                [
                    {
                        "question": "工时费和开户流程分别是什么",
                        "expected_doc_ids": ["fee", "account", "ghost"],
                        "reference_answer": "分别说明",
                    }
                ],
                ensure_ascii=False,
            )
        match = re.search(r"文档 ID：(\S+)", prompt)
        doc_id = match.group(1) if match else "fee"
        return json.dumps(
            {
                "question": f"{doc_id} 相关问题",
                "reference_answer": f"{doc_id} 答案",
            },
            ensure_ascii=False,
        )

    return prompts, complete


def test_generate_question_set_full_pipeline() -> None:
    """生成管线：逐文档出题、未知 doc_id 过滤、重复丢弃、类型统计。"""
    prompts, complete = _fake_complete_factory()

    report = generate_question_set(
        kb_name="pricing",
        description="价格库",
        documents=_DOCS,
        count=10,
        guidance="语气口语化",
        complete=complete,  # type: ignore[arg-type]
    )

    by_type: dict[str, list[QuestionSetItem]] = {}
    for item in report.items:
        by_type.setdefault(item.case_type, []).append(item)
    # 3 篇文档循环出 6 题，同文档问题重复被去重后剩 3 条。
    assert [item.question for item in by_type["single_doc"]] == [
        "fee 相关问题",
        "account 相关问题",
        "promo 相关问题",
    ]
    # 组合题的 ghost 引用被过滤，保留两个真实文档。
    multi = by_type["multi_doc"][0]
    assert multi.expected_doc_ids == ("account", "fee")
    # 边界题携带 forbidden；库外题无期望文档。
    assert by_type["boundary"][0].forbidden == ("200 元/小时",)
    assert by_type["out_of_scope"][0].expected_doc_ids == ()
    # 全部候选进入审核队列。
    assert all(item.status == "pending" for item in report.items)
    assert report.dropped_duplicates == 3
    assert report.type_counts == {
        "single_doc": 3,
        "multi_doc": 1,
        "boundary": 1,
        "out_of_scope": 1,
    }
    # multi_doc 目标 2 实际 1；single_doc 目标 6、去重后实际 3。
    # 补生成（返回空）后如实报告不足。
    assert report.shortfall == {"single_doc": 3, "multi_doc": 1}
    # guidance 原文进入 Prompt。
    assert all("语气口语化" in prompt for prompt in prompts)


def test_generate_question_set_survives_llm_failure() -> None:
    """LLM 全部失败时产出空集合并报告全部类型不足，不抛异常。"""

    def failing_complete(prompt: str) -> str:
        raise RuntimeError("llm down")

    report = generate_question_set(
        kb_name="pricing",
        description="",
        documents=_DOCS,
        count=10,
        guidance="",
        complete=failing_complete,
    )

    assert report.items == ()
    assert report.generated == 0
    assert report.shortfall["single_doc"] == 6


def _item(
    question: str,
    *,
    case_type: str = "single_doc",
    expected: tuple[str, ...] = ("fee",),
    reference: str | None = None,
    forbidden: tuple[str, ...] = (),
) -> QuestionSetItem:
    return QuestionSetItem(
        question=question,
        expected_doc_ids=expected,
        reference_answer=reference,
        case_type=case_type,
        status="accepted",
        forbidden=forbidden,
    )


def _expected_case_id(scope: str, kb_name: str, set_name: str, index: int) -> str:
    """按规格独立重算期望 case id：可读前缀 + 结构化 JSON 的 sha256 截断摘要。"""
    payload = json.dumps([scope, kb_name, set_name, index], ensure_ascii=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{scope}-{kb_name}-{set_name}-{index:03d}-{digest}"


def test_to_eval_cases_ids_do_not_collide_for_hyphenated_fields() -> None:
    """scope/kb 含连字符的两个不同来源不得生成相同 id（回归：拼接歧义碰撞）。"""
    left = to_eval_cases(
        scope="sales-a",
        kb_name="pricing",
        set_name="gen",
        items=[(0, _item("Q"))],
    )
    right = to_eval_cases(
        scope="sales",
        kb_name="a-pricing",
        set_name="gen",
        items=[(0, _item("Q"))],
    )

    assert left[0].id != right[0].id


def test_to_eval_cases_ids_stable_across_repeated_conversion() -> None:
    """同一来源重复转换生成完全相同 id（沉淀幂等的前提）。"""
    items = [(0, _item("Q1", reference="答案。")), (2, _item("Q3"))]

    first = to_eval_cases(scope="global", kb_name="pricing", set_name="s1", items=items)
    second = to_eval_cases(
        scope="global", kb_name="pricing", set_name="s1", items=items
    )

    assert [case.id for case in first] == [case.id for case in second]
    assert [case.id for case in first] == [
        _expected_case_id("global", "pricing", "s1", 0),
        _expected_case_id("global", "pricing", "s1", 2),
    ]


def test_to_eval_cases_ids_stable_with_special_characters() -> None:
    """字段含多连字符、下划线、非 ASCII 字符时 id 仍稳定且互不碰撞。"""
    sources = [
        ("app-x_y", "价格-库", "gen_2026-10-08"),
        ("app-x", "y_价格-库", "gen_2026-10-08"),
        ("app-x_y", "价格-库", "gen_2026-10-08_extra"),
    ]

    ids = [
        to_eval_cases(
            scope=scope,
            kb_name=kb_name,
            set_name=set_name,
            items=[(0, _item("Q"))],
        )[0].id
        for scope, kb_name, set_name in sources
    ]

    assert len(set(ids)) == len(ids)
    for (scope, kb_name, set_name), case_id in zip(sources, ids, strict=True):
        assert case_id == _expected_case_id(scope, kb_name, set_name, 0)


def test_append_eval_dataset_keeps_ambiguous_prefix_sources(tmp_path: Path) -> None:
    """旧格式下 id 碰撞的两个来源分批沉淀到同一数据集：全部保留，不错判重复。"""
    _write_eval_task(tmp_path, "task1")
    tasks_root = tmp_path / "eval_tasks"
    first = to_eval_cases(
        scope="sales-a",
        kb_name="pricing",
        set_name="gen",
        items=[(0, _item("A")), (2, _item("C"))],
    )
    appended, skipped, total = append_eval_dataset(tasks_root, "task1", "ds", first)
    assert (appended, skipped, total) == (2, 0, 2)

    second = to_eval_cases(
        scope="sales",
        kb_name="a-pricing",
        set_name="gen",
        items=[(0, _item("B"))],
    )
    appended, skipped, total = append_eval_dataset(tasks_root, "task1", "ds", second)
    assert (appended, skipped, total) == (1, 0, 3)

    loaded = load_dataset(tasks_root / "task1", "ds")
    assert [case.input for case in loaded] == ["A", "C", "B"]


def test_to_eval_cases_maps_all_case_types() -> None:
    """沉淀映射：gold_doc_ids/key_points 按句拆分/must_refuse/forbidden/稳定 id。"""
    items = [
        (0, _item("A 保多少钱", reference="价格 1100-1400 元。含工时费。")),
        (
            1,
            _item(
                "A 保和 B 保差别",
                case_type="multi_doc",
                expected=("fee", "account"),
            ),
        ),
        (2, _item("工时费 200 够吗", case_type="boundary", forbidden=("200 元/小时",))),
        (3, _item("能帮我订外卖吗", case_type="out_of_scope", expected=())),
    ]

    cases = to_eval_cases(
        scope="global",
        kb_name="pricing",
        set_name="gen-20261001-120000",
        items=items,
    )

    assert [case.id for case in cases] == [
        _expected_case_id("global", "pricing", "gen-20261001-120000", 0),
        _expected_case_id("global", "pricing", "gen-20261001-120000", 1),
        _expected_case_id("global", "pricing", "gen-20261001-120000", 2),
        _expected_case_id("global", "pricing", "gen-20261001-120000", 3),
    ]
    first = cases[0]
    assert first.input == "A 保多少钱"
    assert first.expected is not None
    assert first.expected.gold_doc_ids == ["fee"]
    assert first.expected.key_points == ["价格 1100-1400 元", "含工时费"]
    assert first.meta["reference_answer"] == "价格 1100-1400 元。含工时费。"
    assert first.tags == ["pricing", "single_doc"]
    boundary = cases[2]
    assert boundary.expected is not None
    assert boundary.expected.forbidden == ["200 元/小时"]
    out_of_scope = cases[3]
    assert out_of_scope.expected is not None
    assert out_of_scope.expected.must_refuse is True
    assert out_of_scope.expected.gold_doc_ids == []


def test_to_eval_cases_keeps_original_indices() -> None:
    """id 使用原始问题集下标而非 accepted 过滤后的位置，且含 scope/kb 前缀。"""
    cases = to_eval_cases(
        scope="global",
        kb_name="pricing",
        set_name="s1",
        items=[(0, _item("A")), (2, _item("C"))],
    )

    assert [case.id for case in cases] == [
        _expected_case_id("global", "pricing", "s1", 0),
        _expected_case_id("global", "pricing", "s1", 2),
    ]


def _write_eval_task(root: Path, task_id: str) -> Path:
    task_dir = root / "eval_tasks" / task_id
    (task_dir / "datasets").mkdir(parents=True)
    (task_dir / "config.yaml").write_text("agent_id: demo\n", encoding="utf-8")
    return task_dir


def test_append_eval_dataset_appends_and_dedupes(tmp_path: Path) -> None:
    """追加进数据集文件；同 id 重复沉淀跳过；产出可被 evaluation 加载。"""
    _write_eval_task(tmp_path, "task1")
    cases = to_eval_cases(
        scope="global",
        kb_name="pricing",
        set_name="gen-1",
        items=[(0, _item("Q1")), (1, _item("Q2", reference="答案。"))],
    )
    tasks_root = tmp_path / "eval_tasks"

    appended, skipped, total = append_eval_dataset(
        tasks_root, "task1", "generated", cases
    )
    assert (appended, skipped, total) == (2, 0, 2)

    again, skipped_again, total_again = append_eval_dataset(
        tasks_root, "task1", "generated", cases
    )
    assert (again, skipped_again, total_again) == (0, 2, 2)

    loaded = load_dataset(tasks_root / "task1", "generated")
    assert [case.id for case in loaded] == [
        _expected_case_id("global", "pricing", "gen-1", 0),
        _expected_case_id("global", "pricing", "gen-1", 1),
    ]


def test_append_eval_dataset_dedupes_within_single_input(tmp_path: Path) -> None:
    """单次输入内部出现重复 id 时只保留首条，其余计为跳过。"""
    _write_eval_task(tmp_path, "task1")
    cases = to_eval_cases(
        scope="global",
        kb_name="pricing",
        set_name="gen-1",
        items=[(0, _item("Q1")), (0, _item("Q1 变体")), (1, _item("Q2"))],
    )

    appended, skipped, total = append_eval_dataset(
        tmp_path / "eval_tasks", "task1", "generated", cases
    )

    assert (appended, skipped, total) == (2, 1, 2)
    loaded = load_dataset(tmp_path / "eval_tasks" / "task1", "generated")
    assert [case.input for case in loaded] == ["Q1", "Q2"]


def test_append_eval_dataset_validates_task_and_name(tmp_path: Path) -> None:
    """任务不存在 404；数据集名不合法 400。"""
    cases = to_eval_cases(
        scope="global", kb_name="kb", set_name="s", items=[(0, _item("Q"))]
    )

    with pytest.raises(ServiceError) as missing:
        append_eval_dataset(tmp_path / "eval_tasks", "ghost", "ds", cases)
    assert missing.value.kind.name == "NOT_FOUND"

    _write_eval_task(tmp_path, "task1")
    with pytest.raises(ServiceError) as invalid:
        append_eval_dataset(tmp_path / "eval_tasks", "task1", "bad name!", cases)
    assert invalid.value.kind.name == "INVALID"
