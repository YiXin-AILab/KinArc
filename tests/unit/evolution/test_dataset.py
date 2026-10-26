"""可选 JSON Case 加载工具测试。"""

import json
from pathlib import Path

import pytest
from evolution.task import load_json_cases


def _write(path: Path, cases: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases), encoding="utf-8")


def test_load_json_cases_preserves_complete_case(tmp_path: Path) -> None:
    """公共工具只提取 ID，不得丢失 Task 私有字段。"""
    path = tmp_path / "train.json"
    original = {"id": "task_001", "scenario": {"name": "Alice"}}
    _write(path, [original])

    cases = load_json_cases(path)

    assert cases[0].id == "task_001"
    assert cases[0].content == original


@pytest.mark.parametrize(
    "case",
    [{}, {"id": None}, {"id": ""}, {"id": "   "}, {"id": 3}],
)
def test_load_json_cases_rejects_missing_or_invalid_id(
    tmp_path: Path,
    case: object,
) -> None:
    """Case 必须携带非空字符串规范 ID。"""
    path = tmp_path / "train.json"
    _write(path, [case])

    with pytest.raises(ValueError, match="id"):
        load_json_cases(path)


def test_load_json_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    """单个 Dataset 内的 ID 必须唯一。"""
    path = tmp_path / "train.json"
    _write(path, [{"id": "same"}, {"id": "same"}])

    with pytest.raises(ValueError, match="same"):
        load_json_cases(path)


@pytest.mark.parametrize("raw", [{}, [1], ["case"]])
def test_rejects_invalid_json_shape(tmp_path: Path, raw: object) -> None:
    path = tmp_path / "data.json"
    _write(path, raw)
    with pytest.raises(ValueError):
        load_json_cases(path)
