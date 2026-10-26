"""Tau3 Banking Task 配置测试。"""

from pathlib import Path
from typing import cast

import pytest
from evolution_tasks.tau3_banking._internal.process import (
    Tau3BankingConfig,
    load_banking_config,
)
from pydantic import ValidationError


def _valid_config(tmp_path: Path) -> dict[str, object]:
    tau2 = tmp_path / "tau2"
    tau2.mkdir()
    (tau2 / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.1"\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("redis: {}\n", encoding="utf-8")
    return {
        "tau2_project": tau2,
        "runtime_config": runtime,
        "user_llm": "openai/user-model",
        "judge_llm": "openai/judge-model",
        "host": "127.0.0.1",
        "agent_id": "banking-agent",
        "startup_timeout_seconds": 10,
        "case_timeout_seconds": 60,
        "shutdown_timeout_seconds": 5,
    }


def test_config_accepts_exact_tau2_environment(tmp_path: Path) -> None:
    """只接受 Tau2 1.0.1 与本机 Candidate Server。"""
    config = Tau3BankingConfig.model_validate(_valid_config(tmp_path))

    assert config.tau2_project.name == "tau2"
    assert config.agent_id == "banking-agent"
    assert config.validation_num_trials == 1


@pytest.mark.parametrize("value", [0, -1, 1.5, "4", True])
def test_config_rejects_invalid_validation_num_trials(
    tmp_path: Path, value: object
) -> None:
    """重复采样次数只接受不小于 1 的整数，不能隐式转换。"""
    raw = _valid_config(tmp_path)
    raw["validation_num_trials"] = value

    with pytest.raises(ValidationError, match="validation_num_trials"):
        Tau3BankingConfig.model_validate(raw)


def test_load_config_accepts_validation_num_trials(tmp_path: Path) -> None:
    """settings 可显式声明 Validation 重复采样次数。"""
    task = tmp_path / "tasks" / "smoke"
    task.mkdir(parents=True)
    runtime = task / "runtime.yaml"
    runtime.write_text("redis: {}\n", encoding="utf-8")
    entry_dir = tmp_path / "tasks" / "banking"
    tau2 = entry_dir / "tau2-bench"
    tau2.mkdir(parents=True)
    (tau2 / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.1"\n', encoding="utf-8"
    )

    config = load_banking_config(
        task,
        {
            "runtime_config": "runtime.yaml",
            "user_llm": "openai/user",
            "judge_llm": "openai/judge",
            "validation_num_trials": 4,
        },
        entry_dir=entry_dir,
    )

    assert config.validation_num_trials == 4


def test_load_config_uses_task_runtime_and_entry_dependency(tmp_path: Path) -> None:
    """运行配置属于当前 Task，Tau2 则固定属于入口实现。"""
    task = tmp_path / "tasks" / "smoke"
    task.mkdir(parents=True)
    runtime = task / "runtime.yaml"
    runtime.write_text("redis: {}\n", encoding="utf-8")
    entry_dir = tmp_path / "tasks" / "banking"
    tau2 = entry_dir / "tau2-bench"
    tau2.mkdir(parents=True)
    (tau2 / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.1"\n', encoding="utf-8"
    )

    config = load_banking_config(
        task,
        {
            "runtime_config": "runtime.yaml",
            "user_llm": "openai/user",
            "judge_llm": "openai/judge",
        },
        entry_dir=entry_dir,
    )

    assert config.runtime_config == runtime.resolve()
    assert config.tau2_project == tau2.resolve()
    assert config.case_timeout_seconds == 1200


def test_load_config_rejects_user_tau2_project(tmp_path: Path) -> None:
    """用户不能改写由入口实现固定的 Tau2 位置。"""
    task = tmp_path / "task"
    task.mkdir()
    runtime = task / "runtime.yaml"
    runtime.write_text("redis: {}\n", encoding="utf-8")
    entry_dir = tmp_path / "entry"
    tau2 = entry_dir / "tau2-bench"
    tau2.mkdir(parents=True)
    (tau2 / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.1"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="tau2_project"):
        load_banking_config(
            task,
            {
                "runtime_config": "runtime.yaml",
                "user_llm": "openai/user",
                "tau2_project": "elsewhere",
            },
            entry_dir=entry_dir,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("judge_llm", ""),
        ("host", "0.0.0.0"),
        ("agent_id", "another-agent"),
        ("startup_timeout_seconds", 0),
        ("case_timeout_seconds", -1),
        ("shutdown_timeout_seconds", 0),
    ],
)
def test_config_rejects_invalid_fixed_boundary(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """监听范围、Agent 身份和 timeout 不得被配置放宽。"""
    raw = _valid_config(tmp_path)
    raw[field] = value

    with pytest.raises(ValidationError):
        Tau3BankingConfig.model_validate(raw)


def test_config_rejects_unknown_key(tmp_path: Path) -> None:
    """未知配置必须启动时失败。"""
    raw = _valid_config(tmp_path)
    raw["fallback"] = True

    with pytest.raises(ValidationError):
        Tau3BankingConfig.model_validate(raw)


def test_config_rejects_wrong_tau2_version(tmp_path: Path) -> None:
    """外部环境必须固定 Tau2 1.0.1。"""
    raw = _valid_config(tmp_path)
    (cast(Path, raw["tau2_project"]) / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.2"\n', encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="1.0.1"):
        Tau3BankingConfig.model_validate(raw)


def test_missing_tau2_project_points_to_install_command(tmp_path: Path) -> None:
    """缺少内置 Tau2 时提供精确安装位置和命令。"""
    raw = _valid_config(tmp_path)
    tau2 = cast(Path, raw["tau2_project"])
    (tau2 / "pyproject.toml").unlink()
    tau2.rmdir()
    expected = tmp_path / "tau2-bench"
    raw["tau2_project"] = expected

    with pytest.raises(ValidationError) as caught:
        Tau3BankingConfig.model_validate(raw)

    message = str(caught.value)
    assert str(expected) in message
    assert "cd evolution_tasks/tau3_banking\n" in message
    assert (
        "git clone --branch v1.0.1 --single-branch --depth 1 "
        "https://github.com/sierra-research/tau2-bench.git"
    ) in message
