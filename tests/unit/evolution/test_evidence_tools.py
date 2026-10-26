"""Evolution Agent Train Evidence 工具测试。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from agentsystem.common.errors import ToolError
from agentsystem.common.models import JsonValue
from agentsystem.tool.models import ToolOutput
from evolution.agent.plugins import evidence_tools as evidence_module
from evolution.agent.plugins.evidence_tools import EvidenceTools
from evolution.diagnosis import build_diagnosis_brief
from evolution.improver import ImprovementRequest
from evolution.models import (
    CaseEvaluation,
    EvaluationResult,
    ExecutionMetadata,
    ParentComparison,
)


def _request(tmp_path: Path, *, case_id: str = "train-1") -> ImprovementRequest:
    metadata = ExecutionMetadata(total_tokens=12, duration_seconds=0.5)
    evaluation = EvaluationResult(
        total_score=0.5,
        cases=(
            CaseEvaluation(
                id=case_id,
                result="denied",
                tracelog={"events": [{"type": "tool", "name": "lookup"}]},
                metadata=metadata,
                expected={"status": "approved"},
                score=0.0,
                previous_score=1.0,
                feedback="verification was skipped",
            ),
            CaseEvaluation(
                id="train-2",
                result="approved",
                tracelog={"events": []},
                metadata=metadata,
                expected={"status": "approved"},
                score=1.0,
                previous_score=1.0,
                feedback="ok",
            ),
        ),
        parent_comparison=ParentComparison(
            previous_candidate_id="candidate-0",
            current_candidate_id="candidate-1",
            previous_total_score=1.0,
            current_total_score=0.5,
            total_score_delta=-0.5,
            previous_case_count=2,
            current_case_count=2,
            improved_case_count=0,
            regressed_case_count=1,
            unchanged_case_count=1,
        ),
    )
    return ImprovementRequest(
        objective="improve",
        base_candidate_id="candidate-1",
        changelog_source_candidate_id="candidate-1",
        evidence_candidate_id="candidate-1",
        output_candidate_id="candidate-output",
        evidence_parent_candidate_id=None,
        workspace_dir=tmp_path / "workspace",
        read_only=(),
        train_evaluation=evaluation,
        diagnosis_brief=build_diagnosis_brief("candidate-1", evaluation),
        changelog_path=tmp_path / "changelog.md",
    )


def _content(output: ToolOutput) -> dict[str, object]:
    return cast(dict[str, object], json.loads(output.content))


def test_evidence_uses_bound_candidate_not_workspace(tmp_path: Path) -> None:
    """不同版本 Workspace 不得覆盖绑定的证据，重新绑定后缓存也要切换。"""
    request = _request(tmp_path)
    case = request.train_evaluation.cases[0].model_copy(
        update={
            "result": "B_RESULT",
            "expected": "B_EXPECTED",
            "tracelog": "B_TRACE",
        }
    )
    request = request.model_copy(
        update={
            "base_candidate_id": "A",
            "evidence_candidate_id": "B",
            "evidence_parent_candidate_id": "A",
            "train_evaluation": EvaluationResult(total_score=0, cases=(case,)),
        }
    )
    tools = EvidenceTools()
    tools.bind(request)
    result = _content(tools.read_case_evidence("train-1"))
    assert result["result"] == "B_RESULT"
    assert result["expected"] == "B_EXPECTED"
    assert "B_TRACE" in tools.read_case_tracelog("train-1").content
    tools.bind(
        request.model_copy(
            update={
                "evidence_candidate_id": "C",
                "train_evaluation": EvaluationResult(
                    total_score=0,
                    cases=(case.model_copy(update={"tracelog": "C_TRACE"}),),
                ),
            }
        )
    )
    assert "C_TRACE" in tools.read_case_tracelog("train-1").content
    assert "B_TRACE" not in tools.read_case_tracelog("train-1").content
    tools.unbind()
    with pytest.raises(ToolError) as error:
        tools.read_case_evidence("train-1")
    assert error.value.code == "request_not_bound"


def test_read_case_evidence_keeps_scores_but_defers_trace(tmp_path: Path) -> None:
    """轨迹不能随评分证据全量进入上下文，其他字段仍须完整。"""
    tools = EvidenceTools()
    tools.bind(_request(tmp_path))

    result = _content(tools.read_case_evidence("train-1"))
    trace = result.pop("tracelog")
    assert isinstance(trace, dict)
    assert trace["total_lines"] == 8
    assert "read_case_tracelog" in str(trace["hint"])
    assert "truncate=false" in str(trace["hint"])
    assert "limit=1" in str(trace["hint"])
    assert "next_offset" in str(trace["hint"])
    assert "截断长行" in str(trace["hint"])

    assert result == {
        "id": "train-1",
        "result": "denied",
        "metadata": {"total_tokens": 12, "duration_seconds": 0.5},
        "expected": {"status": "approved"},
        "score": 0.0,
        "previous_score": 1.0,
        "feedback": "verification was skipped",
    }


def test_read_case_evidence_includes_failure_kind_only_when_present(
    tmp_path: Path,
) -> None:
    """Evaluator 提供失败类别时随证据暴露，缺省时不占位。"""
    tools = EvidenceTools()
    request = _request(tmp_path)
    with_kind = request.train_evaluation.cases[0].model_copy(
        update={"failure_kind": "action_mismatch:apply_for_credit_card"}
    )
    tools.bind(
        request.model_copy(
            update={
                "train_evaluation": request.train_evaluation.model_copy(
                    update={"cases": (with_kind,)}
                )
            }
        )
    )
    result = _content(tools.read_case_evidence("train-1"))
    assert result["failure_kind"] == "action_mismatch:apply_for_credit_card"

    tools.bind(request)
    result = _content(tools.read_case_evidence("train-2"))
    assert "failure_kind" not in result


def test_read_case_evidence_rejects_unknown_id(tmp_path: Path) -> None:
    """未知 case_id 不得返回另一个 Case 或空成功。"""
    tools = EvidenceTools()
    tools.bind(_request(tmp_path))

    with pytest.raises(ToolError) as captured:
        tools.read_case_evidence("missing")

    assert captured.value.code == "case_not_found"


def _request_with_trace(tmp_path: Path, trace: JsonValue) -> ImprovementRequest:
    request = _request(tmp_path)
    evaluation = request.train_evaluation
    first = evaluation.cases[0].model_copy(update={"tracelog": trace})
    return request.model_copy(
        update={
            "train_evaluation": evaluation.model_copy(
                update={"cases": (first, *evaluation.cases[1:])}
            )
        }
    )


@pytest.mark.parametrize("length", [1999, 2000, 2001, 30000])
def test_tracelog_truncates_only_long_lines(tmp_path: Path, length: int) -> None:
    """逐行截断不应改变原文，取消截断不能偷偷保留响应总量上限。"""
    tools = EvidenceTools()
    request = _request_with_trace(tmp_path, "中" * length + "\nend")
    before = request.model_dump_json()
    tools.bind(request)
    suffix = (
        f" [read_case_tracelog 展示截断：原文共 {length} 字符；"
        "完整读取：offset=0, limit=1, truncate=false]"
        if length > 2000
        else ""
    )
    assert tools.read_case_tracelog("train-1", limit=1).content == (
        "--- train-1/tracelog total_lines=2\n1\t"
        + "中" * min(length, 2000)
        + suffix
        + "\n--- next_offset=1 eof=false"
    )
    assert tools.read_case_tracelog("train-1", limit=2, truncate=False).content == (
        "--- train-1/tracelog total_lines=2\n1\t"
        + "中" * length
        + "\n2\tend\n--- next_offset=2 eof=true"
    )
    assert request.model_dump_json() == before


@pytest.mark.parametrize(
    ("trace", "expected"),
    [
        ("", []),
        ("\n", [""]),
        ("a\n\n", ["a", ""]),
        ("a\r\nb\rc\n", ["a", "b", "c"]),
        ("a\u2028b\u0085c", ["a\u2028b\u0085c"]),
        (None, ["null"]),
        ([], ["[]"]),
        ({}, ["{}"]),
        ({"z": "a\nb", "a": 1}, ["{", '  "z": "a\\nb",', '  "a": 1', "}"]),
        (
            "--- literal\n\t [truncated: original] ",
            ["--- literal", "\t [truncated: original] "],
        ),
    ],
)
def test_tracelog_view_preserves_line_semantics(
    tmp_path: Path,
    trace: JsonValue,
    expected: list[str],
) -> None:
    """格式化 JSON 不得解码嵌套换行或对原文额外折行和去空白。"""
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, trace))
    body = [f"{i}\t{line}" for i, line in enumerate(expected, 1)]
    assert tools.read_case_tracelog("train-1").content == "\n".join(
        [
            f"--- train-1/tracelog total_lines={len(expected)}",
            *body,
            f"--- next_offset={len(expected)} eof=true",
        ]
    )
    evidence = _content(tools.read_case_evidence("train-1"))
    assert cast(dict[str, object], evidence["tracelog"])["total_lines"] == len(expected)


@pytest.mark.parametrize(
    ("offset", "body", "next_offset", "eof"),
    [
        (0, "1\ta\n", 1, "false"),
        (1, "2\tb\n", 2, "true"),
        (2, "", 2, "true"),
        (9, "", 9, "true"),
    ],
)
def test_tracelog_pagination(
    tmp_path: Path,
    offset: int,
    body: str,
    next_offset: int,
    eof: str,
) -> None:
    """页边界及越界读取不得重复或丢失轨迹行。"""
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, "a\nb"))
    assert tools.read_case_tracelog("train-1", offset, 1).content == (
        "--- train-1/tracelog total_lines=2\n"
        + body
        + f"--- next_offset={next_offset} eof={eof}"
    )


def test_tracelog_groups_message_fields_without_changing_values(tmp_path: Path) -> None:
    """消息正文与工具参数各占一行，跨页读取仍能还原完整 JSON。"""
    message: JsonValue = {
        "role": "assistant",
        "id": "message-1",
        "reasoning": "思考\n下一行",
        "content": '回复含 "引号" 和 \\ 路径',
        "tool_calls": [
            {"id": "call-1", "name": "lookup", "arguments": {"query": "银行"}}
        ],
    }
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, message))
    lines: list[str] = []
    for offset in (0, 3):
        page = tools.read_case_tracelog(
            "train-1", offset=offset, limit=3, truncate=False
        ).content
        lines.extend(line.split("\t", 1)[1] for line in page.splitlines()[1:-1])
    assert lines == [
        "{",
        '  "role": "assistant", "id": "message-1",',
        '  "reasoning": "思考\\n下一行",',
        '  "content": "回复含 \\"引号\\" 和 \\\\ 路径",',
        '  "tool_calls": [{"id": "call-1", "name": "lookup", '
        '"arguments": {"query": "银行"}}]',
        "}",
    ]
    assert json.loads("\n".join(lines)) == message
    trace = _content(tools.read_case_evidence("train-1"))["tracelog"]
    assert isinstance(trace, dict)
    assert trace["total_lines"] == 6


def test_tracelog_auto_page_reads_short_trace_in_one_call(tmp_path: Path) -> None:
    """null 使用自动分页，省略 limit 则使用默认行数。"""
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, "line\n" * 600))
    content = tools.read_case_tracelog("train-1", limit=None).content
    assert "600\tline\n--- next_offset=600 eof=true" in content
    assert tools.read_case_tracelog("train-1").content.endswith(
        "--- next_offset=500 eof=false"
    )
    assert tools.read_case_tracelog("train-1", limit=100).content.endswith(
        "--- next_offset=100 eof=false"
    )


def test_tracelog_auto_pages_bound_output_without_losing_lines(tmp_path: Path) -> None:
    """预算按展示字符计算；续读没有重复、遗漏或不前进的页。"""
    tools = EvidenceTools()
    lines = ["中" * 3000 for _ in range(25)]
    tools.bind(_request_with_trace(tmp_path, "\n".join(lines)))
    offset = 0
    seen: list[int] = []
    while offset < len(lines):
        content = tools.read_case_tracelog("train-1", offset=offset, limit=None).content
        body = content.splitlines()[1:-1]
        assert body
        assert sum(len(line) + 1 for line in body) <= 20_000
        seen.extend(int(line.split("\t", 1)[0]) for line in body)
        offset += len(body)
        assert content.endswith(
            f"--- next_offset={offset} eof={str(offset == len(lines)).lower()}"
        )
    assert seen == list(range(1, 26))


def test_auto_page_returns_oversized_full_line_alone(tmp_path: Path) -> None:
    """单行全文超过预算也必须完整返回并推进游标。"""
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, "x" * 30_000 + "\nend"))
    content = tools.read_case_tracelog("train-1", limit=None, truncate=False).content
    assert "1\t" + "x" * 30_000 in content
    assert content.endswith("--- next_offset=1 eof=false")
    assert tools.read_case_tracelog("train-1", offset=1).content.endswith(
        "2\tend\n--- next_offset=2 eof=true"
    )


def test_tracelog_cache_is_scoped_to_request(tmp_path: Path) -> None:
    """重绑定同名 Case 不得继续读取上个 Candidate 的缓存。"""
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, "old"))
    assert "1\told" in tools.read_case_tracelog("train-1").content
    tools.bind(_request_with_trace(tmp_path, "new"))
    assert "1\tnew" in tools.read_case_tracelog("train-1").content
    tools.unbind()
    with pytest.raises(ToolError, match="尚未绑定"):
        tools.read_case_tracelog("train-1")


def test_rebind_during_view_build_cannot_publish_old_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成中的旧轨迹不能污染新请求缓存，解绑也不能等到生成完成。"""
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, "old"))
    started, release = Event(), Event()
    original = evidence_module._trace_lines

    def build(value: JsonValue) -> tuple[str, ...]:
        if value == "old":
            started.set()
            assert release.wait(5)
        return original(value)

    monkeypatch.setattr(evidence_module, "_trace_lines", build)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(tools.read_case_tracelog, "train-1")
        try:
            assert started.wait(5)
            tools.bind(_request_with_trace(tmp_path, "new"))
        finally:
            release.set()
        with pytest.raises(ToolError) as error:
            pending.result(timeout=5)
        assert error.value.code == "request_not_bound"
    assert "1\tnew" in tools.read_case_tracelog("train-1").content


def test_case_view_is_reused_between_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每页重新生成大轨迹会重复消耗序列化时间。"""
    tools = EvidenceTools()
    tools.bind(_request_with_trace(tmp_path, "first\nsecond"))
    calls = 0
    original = evidence_module._trace_lines

    def build(value: JsonValue) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(evidence_module, "_trace_lines", build)
    tools.read_case_evidence("train-1")
    assert "1\tfirst" in tools.read_case_tracelog("train-1", limit=1).content
    assert "2\tsecond" in tools.read_case_tracelog("train-1", offset=1).content
    assert calls == 1


@pytest.mark.parametrize(
    ("offset", "limit", "code"),
    [
        (-1, 1, "invalid_offset"),
        (0, 0, "invalid_limit"),
        (True, 1, "invalid_offset"),
        (0, True, "invalid_limit"),
    ],
)
def test_tracelog_rejects_invalid_page(
    tmp_path: Path,
    offset: int,
    limit: int,
    code: str,
) -> None:
    """布尔值不应被当作合法整数分页参数。"""
    tools = EvidenceTools()
    tools.bind(_request(tmp_path))
    with pytest.raises(ToolError) as error:
        tools.read_case_tracelog("train-1", offset, limit)
    assert error.value.code == code


def test_rebind_and_unbind_isolate_requests(tmp_path: Path) -> None:
    """跨轮残留会把上一个 Candidate 的 Train 证据暴露给下一轮。"""
    tools = EvidenceTools()

    with pytest.raises(ToolError) as before_bind:
        tools.read_case_evidence("current")
    assert before_bind.value.code == "request_not_bound"

    tools.bind(_request(tmp_path, case_id="current"))
    case = _content(tools.read_case_evidence("current"))
    assert case["id"] == "current"

    tools.unbind()
    with pytest.raises(ToolError) as after_unbind:
        tools.read_case_evidence("current")
    assert after_unbind.value.code == "request_not_bound"
