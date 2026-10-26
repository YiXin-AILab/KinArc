"""server.platform.evaluation.runner 评测 Run 注册表错误处理测试。"""

import json
import logging
from pathlib import Path

import pytest
from server.platform.evaluation.runner import EvaluationRunRegistry
from server.platform.evaluation.store import EvaluationStore

_CONFIG = """agent_id: fake-agent
dataset: main
judge_llm:
  model: openai/test-model
  api_key: test-key
  base_url: http://127.0.0.1:9/v1
"""


def _write_task(root: Path, task_id: str = "task_one") -> Path:
    task_dir = root / "eval_tasks" / task_id
    (task_dir / "datasets").mkdir(parents=True)
    (task_dir / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (task_dir / "datasets" / "main.json").write_text(
        json.dumps([{"id": "c1", "input": "你好"}]),
        encoding="utf-8",
    )
    return task_dir


def _runner_records(
    caplog: pytest.LogCaptureFixture,
    level: int,
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if (
            record.name == "server.platform.evaluation.runner"
            and record.levelno >= level
        )
    ]


def test_list_runs_skips_corrupt_runs_with_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_task(tmp_path)
    store = EvaluationStore(tasks_root=tmp_path / "eval_tasks")
    store.create_run_dir("task_one", "run-valid")
    runs_dir = tmp_path / "eval_tasks" / "task_one" / "runs"
    broken_json = runs_dir / "run-brokenjson"
    broken_json.mkdir()
    (broken_json / "meta.json").write_text("not json", encoding="utf-8")
    broken_status = runs_dir / "run-brokenstatus"
    broken_status.mkdir()
    (broken_status / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "run-brokenstatus",
                "task_id": "task_one",
                "status": "bogus",
                "created_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    registry = EvaluationRunRegistry(store=store)
    with caplog.at_level(logging.DEBUG, logger="server.platform.evaluation.runner"):
        views = registry.list_runs("task_one")
    # 损坏 Run 被跳过但不影响列表其余部分。
    assert [view.run_id for view in views] == ["run-valid"]
    # 预期失败（损坏 meta）记录无堆栈 warning，不产生 error 级日志。
    assert _runner_records(caplog, logging.ERROR) == []
    warnings = _runner_records(caplog, logging.WARNING)
    assert len(warnings) == 2
    text = "\n".join(record.getMessage() for record in warnings)
    assert "run-brokenjson" in text
    assert "run-brokenstatus" in text


def test_list_runs_logs_unexpected_error_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_task(tmp_path)
    store = EvaluationStore(tasks_root=tmp_path / "eval_tasks")
    store.create_run_dir("task_one", "run-one")
    registry = EvaluationRunRegistry(store=store)

    secret = "-".join(["disk", "token", "secret"])

    def _boom(self: EvaluationStore, run_dir: Path) -> dict[str, object]:
        raise RuntimeError(f"cause={secret}")

    monkeypatch.setattr(EvaluationStore, "read_meta", _boom)
    with caplog.at_level(logging.DEBUG, logger="server.platform.evaluation.runner"):
        views = registry.list_runs("task_one")
    # 单个 Run 的未预期失败不终止整个列表。
    assert views == ()
    # 未预期异常在最终所有权边界恰好记录一次完整脱敏堆栈。
    errors = _runner_records(caplog, logging.ERROR)
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "evaluation_run_unexpected" in message
    assert "operation=evaluation.list_runs" in message
    assert "run-one" in message
    assert errors[0].exc_info is not None
    # 异常正文脱敏，原始敏感内容不得进入日志。
    assert "disk-token-secret" not in caplog.text
