"""原生 Banking 评测的显式接入配置。"""

import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

UPSTREAM_TAG = "v1.0.1"
AGENT_NAME = "agentsystem_banking"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TAU2_PROJECT = Path(__file__).resolve().parent / "tau2-bench"
type PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]


class IntegrationConfig(BaseModel):
    """明确接入路径，凭证仅从进程环境读取。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    agent_dir: Path
    runtime_config: Path
    runtime_python: Path
    startup_timeout_seconds: PositiveSeconds = 60
    shutdown_timeout_seconds: PositiveSeconds = 20
    http_timeout_seconds: PositiveSeconds = 1200

    @field_validator("agent_dir", "runtime_config", "runtime_python")
    @classmethod
    def absolute_path(cls, value: Path, info: ValidationInfo) -> Path:
        """按配置文件目录定位相对路径，不解析虚拟环境符号链接。"""
        if not value.is_absolute():
            if not isinstance(info.context, Path):
                raise ValueError(
                    "relative integration paths require a config directory"
                )
            return Path(os.path.abspath(info.context / value))
        return value


def load_config(path: Path | None = None) -> IntegrationConfig:
    """加载接入配置，不隐式读取 dotenv 文件。

    Args:
        path: 可选的 JSON 配置路径。未指定时使用
            ``TAU2_AGENTSYSTEM_CONFIG`` 或各项 ``TAU2_*`` 环境变量。

    Returns:
        校验后的路径和超时配置。

    Raises:
        ValueError: 相对路径缺少配置目录，或配置值无效。
    """
    if path is None and os.environ.get("TAU2_AGENTSYSTEM_CONFIG"):
        path = Path(os.environ["TAU2_AGENTSYSTEM_CONFIG"])
    if path is not None:
        return IntegrationConfig.model_validate_json(
            path.read_text(encoding="utf-8"), context=path.absolute().parent
        )
    return IntegrationConfig.model_validate_strings(
        {
            name: os.environ[f"TAU2_{name.upper()}"]
            for name in IntegrationConfig.model_fields
            if f"TAU2_{name.upper()}" in os.environ
        },
        context=Path(__file__).resolve().parent,
    )


def validate_run(config: Mapping[str, object]) -> None:
    """启动 case 前拒绝不支持的原生评测参数。

    Args:
        config: 序列化后的 Tau2 RunConfig 字段。

    Raises:
        ValueError: 领域、模式、超时、并发、模型或 Agent 模型参数不受支持。
    """
    if config.get("domain") != "banking_knowledge":
        raise ValueError("agentsystem_banking requires banking_knowledge")
    if config.get("audio_native_config") or config.get("solo_mode"):
        raise ValueError("only text half-duplex evaluation is supported")
    _positive(config.get("timeout"), "timeout")
    concurrency = config.get("max_concurrency")
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("max_concurrency must be a positive integer")
    if not config.get("llm_agent"):
        raise ValueError("agent-llm is required")
    arguments = config.get("llm_args_agent") or {}
    if not isinstance(arguments, dict) or arguments.keys() - {"temperature"}:
        raise ValueError("agent-llm-args only supports temperature")
    temperature = arguments.get("temperature", 0)
    if type(temperature) not in {int, float} or not math.isfinite(temperature):
        raise ValueError("temperature must be finite")
    if not 0 <= temperature <= 2:
        raise ValueError("temperature must be between 0 and 2")


def _positive(value: object, name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")
