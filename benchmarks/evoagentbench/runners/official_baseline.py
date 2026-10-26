"""固定官方执行框架的基线，包含可审计的独立试次。"""

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Protocol

import yaml
from agentsystem.config.models import AgentConfig

from benchmarks.evoagentbench.runners.official_baseline_worker import (
    NANOBOT_VERSION,
    resolve_environment,
)


class OfficialDependencies(Protocol):
    """各领域配置共用的最小环境协议。"""

    upstream_repo: Path
    upstream_python: Path
    upstream_revision: str


def load_baseline_config(path: Path) -> dict[str, Any]:
    """执行前校验凭证来源和固定的执行框架策略。"""
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict) or set(value) != {
        "nanobot_version",
        "max_retries",
        "agent",
    }:
        raise ValueError("Baseline requires nanobot_version, max_retries and agent")
    if value["nanobot_version"] != NANOBOT_VERSION or value["max_retries"] != 0:
        raise ValueError("Baseline requires pinned Nanobot and max_retries=0")
    agent = value["agent"]
    allowed = {
        "model",
        "provider",
        "providers",
        "maxTokens",
        "temperature",
        "contextWindowTokens",
        "max_tool_iterations",
        "reasoningEffort",
    }
    if not isinstance(agent, dict) or set(agent) - allowed:
        raise ValueError("Unsupported baseline agent configuration")
    if not all(
        key in agent
        for key in ("model", "provider", "providers", "maxTokens", "temperature")
    ):
        raise ValueError("Baseline model and sampling parameters must be explicit")
    if agent["provider"] in ("openai_codex", "anthropic_oauth"):
        raise ValueError("Baseline usage recorder requires the LiteLLM provider")
    for provider in agent["providers"].values():
        for key, item in provider.items():
            if (
                key not in {"apiKey", "apiBase"}
                or not isinstance(item, str)
                or not (item.startswith("${") and item.endswith("}"))
            ):
                raise ValueError(
                    "Provider credentials and endpoints must be environment references"
                )
    return value


def validate_baseline_model(
    config_path: Path, agent_config: AgentConfig
) -> dict[str, Any]:
    """要求供应商模型、采样参数、输出与上下文预算一致。"""
    base = resolve_environment(load_baseline_config(config_path)["agent"])
    candidate = agent_config.llm_model

    # 仅去除已知传输前缀，保留带供应商限定的模型 ID。
    def normalized(model: str) -> str:
        prefix, separator, suffix = model.partition("/")
        return (
            suffix
            if separator and prefix in {"openai", "custom", "openrouter", "hosted_vllm"}
            else model
        )

    provenance = {
        "model": normalized(base["model"]),
        "temperature": base["temperature"],
        "max_output_tokens": base["maxTokens"],
        "context_window": base.get("contextWindowTokens"),
    }
    expected = {
        "model": normalized(candidate.model),
        "temperature": candidate.temperature,
        "max_output_tokens": candidate.max_output_tokens,
        "context_window": candidate.context_window,
    }
    if provenance != expected:
        raise ValueError("Baseline and candidate model/sampling budgets differ")
    endpoint = base["providers"][base["provider"]]["apiBase"].rstrip("/")
    if endpoint != str(candidate.base_url or "").rstrip("/"):
        raise ValueError("Baseline and candidate provider endpoints differ")
    provenance["endpoint_sha256"] = hashlib.sha256(endpoint.encode()).hexdigest()
    return provenance


def preflight_baseline(settings: OfficialDependencies, config_path: Path) -> None:
    """校验显式基线配置与已安装依赖。"""
    resolve_environment(load_baseline_config(config_path))
    preflight(settings)


def preflight(settings: OfficialDependencies) -> None:
    """调用模型前检查指定版本的源码和依赖可执行文件。"""
    revision = subprocess.check_output(
        ["git", "-C", str(settings.upstream_repo), "rev-parse", "HEAD"],
        text=True,
        timeout=10,
    ).strip()
    if revision != settings.upstream_revision:
        raise ValueError("Official source revision mismatch")
    dirty = subprocess.check_output(
        [
            "git",
            "-C",
            str(settings.upstream_repo),
            "status",
            "--porcelain",
            "--",
            "benchmark/src",
        ],
        text=True,
        timeout=10,
    )
    if dirty.strip():
        raise ValueError("Official adapter sources must be clean")
    subprocess.run(
        [
            str(settings.upstream_python),
            "-c",
            "import importlib.metadata; import nanobot.cli.commands; "
            "assert importlib.metadata.version('nanobot-ai') == '"
            + NANOBOT_VERSION
            + "'",
        ],
        check=True,
        timeout=60,
        capture_output=True,
    )
