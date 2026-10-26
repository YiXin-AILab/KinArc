"""只读实验状态与评分汇总测试。"""

import json
import re
import sys
from pathlib import Path

import pytest
from evolution.cli import run_cli
from evolution.models import CaseEvaluation, EvaluationResult, ExecutionMetadata


def _save(
    root: Path,
    index: int,
    role: str,
    scores: tuple[float, ...],
    *,
    ids: tuple[str, ...] | None = None,
) -> Path:
    directory = root / ".evolution/candidates" / f"candidate-20260917T1200{index:02}"
    output = directory / role / "evaluation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = directory / "parent"
    if not parent.exists():
        parent.write_text(f"candidate-20260917T1200{index - 1:02}" if index > 1 else "")
    evaluation = EvaluationResult(
        total_score=sum(scores) / len(scores) if scores else 0,
        cases=tuple(
            CaseEvaluation(
                id=ids[i] if ids else str(i),
                score=score,
                result="done",
                expected={},
                feedback="",
                tracelog={},
                metadata=ExecutionMetadata(total_tokens=0, duration_seconds=0),
            )
            for i, score in enumerate(scores)
        ),
    )
    output.write_text(evaluation.model_dump_json())
    return directory


def _status(root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert run_cli(["status", str(root)], project_root=root, environ={}) == 0
    return capsys.readouterr().out


def test_status_lists_all_candidates_without_loading_task_or_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """无配置或代码文件也能查看评分，缺阶段不补跑，所有已有文件不变。"""
    third = _save(tmp_path, 3, "train", (1, 1, 0, 0))
    _save(tmp_path, 3, "validation", (1, 1))
    _save(tmp_path, 3, "test", (0, 1))
    _save(tmp_path, 1, "train", (1, 0, 0, 0))
    _save(tmp_path, 1, "validation", (1, 0))
    _save(tmp_path, 2, "train", (1, 1, 1, 0))
    (third / "parent").write_text("candidate-20260917T120001")
    pending = third.parent / "candidate-20260917T120004"
    pending.mkdir()
    (third.parent / ".candidate-20260917T120005.staging").mkdir()
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    text = _status(tmp_path, capsys)

    assert "候选总数：4" in text
    assert "验证最佳：candidate-20260917T120003" in text
    rows = [line.split() for line in text.splitlines() if line[:1].isdigit()]
    assert rows == [
        ["1", "candidate-20260917T120001", "25%|1/4|-", "50%|1/2|-", "-"],
        ["2", "candidate-20260917T120002", "75%|3/4|+50%", "-", "-"],
        [
            "3",
            "candidate-20260917T120003",
            "50%|2/4|+25%",
            "100%|2/2|+50%",
            "50%|1/2|-",
        ],
        ["4", "candidate-20260917T120004", "-", "-", "-"],
    ]
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*")
        if p.is_file()
    } == before
    assert not (pending / "changelog.md").exists()


def test_status_distinguishes_partial_score_and_accuracy_and_case_sets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """部分分不计正确；不同 Case 集合不计算变化；空集合不计算准确率。"""
    _save(tmp_path, 1, "train", (0, 0.5))
    _save(tmp_path, 2, "train", (1, 0.5))
    _save(tmp_path, 3, "train", (1, 0), ids=("other-a", "other-b"))
    _save(tmp_path, 4, "train", ())
    text = _status(tmp_path, capsys)
    assert "25%|0/2|-" in text
    assert "75%|1/2|+50%" in text
    assert "50%|1/2|-" in text
    assert "0%|0/0|-" in text
    assert "百分点" in text


def test_status_empty_experiment_does_not_initialize(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "暂无候选" in _status(tmp_path, capsys)
    assert not (tmp_path / ".evolution").exists()


def test_status_does_not_compare_with_older_non_parent_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _save(tmp_path, 1, "validation", (0, 1))
    _save(tmp_path, 2, "train", (1, 1))
    _save(tmp_path, 3, "validation", (1, 1))
    text = _status(tmp_path, capsys)
    assert "100%|2/2|-" in text
    assert "+50%" not in text


def test_status_marks_invalid_results_and_keeps_other_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = _save(tmp_path, 1, "train", (1,))
    (directory / "train/evaluation.json").write_text(json.dumps({"bad": True}))
    _save(tmp_path, 2, "train", (1,))
    text = _status(tmp_path, capsys)
    assert "错误" in text
    assert "100%|1/1|-" in text
    assert "candidate-20260917T120001/train" in text


def test_status_rounds_decline_and_ties_keep_earlier_best(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _save(tmp_path, 1, "validation", (1, 1, 1))
    _save(tmp_path, 2, "validation", (1, 1, 1))
    _save(tmp_path, 3, "validation", (1, 0, 1))
    text = _status(tmp_path, capsys)
    assert "100%|3/3|0%" in text
    assert "66.67%|2/3|-33.33%" in text
    assert "验证最佳：candidate-20260917T120001" in text


def test_status_missing_task_reports_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            ["status", str(tmp_path / "missing")], project_root=tmp_path, environ={}
        )
        == 2
    )
    assert "missing" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()


def test_status_bolds_column_maxima_and_ties_only_in_terminal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """各列独立按原始总分加粗，并列与零分有效，样式不改变列宽。"""
    _save(tmp_path, 1, "train", (1, 0.5))
    _save(tmp_path, 2, "train", (0.75, 0.75))
    _save(tmp_path, 1, "validation", (1, 0))
    _save(tmp_path, 2, "validation", (1, 1))
    _save(tmp_path, 1, "test", (0,))
    _save(tmp_path, 2, "test", (0,))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    plain = _status(tmp_path, capsys)
    assert "\x1b" not in plain

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    styled = _status(tmp_path, capsys)

    assert re.findall(r"\x1b\[1m(.*?)\x1b\[0m", styled) == [
        "75%|1/2|-",
        "0%|0/1|-",
        "75%|0/2|-50%",
        "100%|2/2|+50%",
        "0%|0/1|0%",
    ]
    assert styled.replace("\x1b[1m", "").replace("\x1b[0m", "") == plain
