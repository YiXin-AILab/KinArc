"""YAML 配置加载与环境变量插值测试。"""

from pathlib import Path

import pytest
from agentsystem.config.loader import interpolate_environment, load_yaml_config
from agentsystem.config.models import (
    AgentConfig,
    ClusterRedisConfig,
    RuntimeConfig,
)


def test_interpolation_recurses_through_nested_collections() -> None:
    """字典、列表和 tuple 内的所有占位符都必须被替换。"""
    value = {
        "url": "redis://${REDIS_HOST}:6379/0",
        "items": ["${AGENT_NAME}", ("prefix-${AGENT_NAME}",)],
    }

    interpolated = interpolate_environment(
        value,
        {"REDIS_HOST": "localhost", "AGENT_NAME": "demo"},
    )

    assert interpolated == {
        "url": "redis://localhost:6379/0",
        "items": ["demo", ("prefix-demo",)],
    }


def test_interpolation_rejects_missing_environment_variable() -> None:
    """缺失变量必须在模型校验前以明确异常失败。"""
    with pytest.raises(ValueError, match="MISSING_KEY"):
        interpolate_environment("${MISSING_KEY}", {})


def test_yaml_loader_converts_interpolated_api_key_to_secret(
    tmp_path: Path,
) -> None:
    """YAML 中插值后的模型密钥必须由配置模型转成 SecretStr。"""
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
agent_id: agent-1
agent_name: Example Agent
prompt: application/example/prompt.yaml
enabled_skills: [stock-analysis]
output_mode: complete
outbound:
  type: log
llm_model:
  type: litellm
  model: openai/gpt-5-mini
  api_key: ${LLM_API_KEY}
""".strip(),
        encoding="utf-8",
    )

    config = load_yaml_config(
        config_path,
        AgentConfig,
        environ={"LLM_API_KEY": "top-secret-key"},
    )

    api_key = config.llm_model.api_key
    assert config.enabled_skills == ("stock-analysis",)
    assert api_key is not None
    assert api_key.get_secret_value() == "top-secret-key"
    assert "top-secret-key" not in repr(config)


def test_yaml_loader_rejects_non_mapping_root(tmp_path: Path) -> None:
    """配置文件根节点必须是 mapping。"""
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text("- invalid\n- root\n", encoding="utf-8")

    with pytest.raises(ValueError, match="根节点"):
        load_yaml_config(config_path, AgentConfig, environ={})


def test_yaml_loader_rejects_duplicate_plugin_options_key(tmp_path: Path) -> None:
    """同名 Plugin 参数不能被 YAML 后值静默覆盖。"""
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
agent_id: agent-1
agent_name: Example Agent
prompt: prompt/system.md
outbound:
  type: log
plugin_options:
  audit:
    enabled: true
  audit:
    enabled: false
llm_model:
  model: openai/gpt-5-mini
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"plugin_options.*audit"):
        load_yaml_config(config_path, AgentConfig, environ={})


def test_yaml_loader_accepts_agent_capability_lists(tmp_path: Path) -> None:
    """Agent YAML 使用统一 enabled 字段加载三类 capability。"""
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
agent_id: loader-test
agent_name: Loader Test
prompt: prompt.md
outbound:
  type: log
enabled_tools: ["workspace_*"]
enabled_middlewares: []
enabled_context_providers: [current-datetime]
llm_model:
  type: litellm
  model: openai/test
""".strip(),
        encoding="utf-8",
    )

    config = load_yaml_config(config_path, AgentConfig, environ={})

    assert config.enabled_tools == ("workspace_*",)
    assert config.enabled_middlewares == ()
    assert config.enabled_context_providers == ("current-datetime",)


def test_yaml_loader_rejects_removed_context_provider_field(
    tmp_path: Path,
) -> None:
    """Agent YAML 不保留旧 context_providers 兼容字段。"""
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
agent_id: loader-test
agent_name: Loader Test
prompt: prompt.md
outbound:
  type: log
context_providers: [current-datetime]
llm_model:
  type: litellm
  model: openai/test
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_yaml_config(config_path, AgentConfig, environ={})


def test_yaml_loader_only_checks_plugin_options_first_level_keys(
    tmp_path: Path,
) -> None:
    """专用重复检查不扩大到 Plugin 自己拥有的嵌套配置。"""
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
agent_id: agent-1
agent_name: Example Agent
prompt: prompt/system.md
outbound:
  type: log
plugin_options:
  audit:
    enabled: true
    enabled: false
  shared:
    limit: 10
llm_model:
  model: openai/gpt-5-mini
""".strip(),
        encoding="utf-8",
    )

    config = load_yaml_config(config_path, AgentConfig, environ={})

    assert config.plugin_options == {
        "audit": {"enabled": False},
        "shared": {"limit": 10},
    }


def test_runtime_loader_accepts_cluster_nodes_from_environment(
    tmp_path: Path,
) -> None:
    """同一 Runtime 配置入口改用 Cluster 段时应加载多个 seed。"""
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(
        """
redis:
  type: cluster
  startup_nodes:
    - host: ${REDIS_CLUSTER_NODE_1_HOST}
      port: ${REDIS_CLUSTER_NODE_1_PORT}
    - host: ${REDIS_CLUSTER_NODE_2_HOST}
      port: ${REDIS_CLUSTER_NODE_2_PORT}
  username: ${REDIS_USERNAME}
  password: ${REDIS_PASSWORD}
asset:
  max_file_bytes: 104857600
asset_store:
  type: local
""".strip(),
        encoding="utf-8",
    )
    config = load_yaml_config(
        runtime_path,
        RuntimeConfig,
        environ={
            "REDIS_CLUSTER_NODE_1_HOST": "redis-1",
            "REDIS_CLUSTER_NODE_1_PORT": "7000",
            "REDIS_CLUSTER_NODE_2_HOST": "redis-2",
            "REDIS_CLUSTER_NODE_2_PORT": "7001",
            "REDIS_USERNAME": "",
            "REDIS_PASSWORD": "",
        },
    )

    assert isinstance(config.redis, ClusterRedisConfig)
    assert [(node.host, node.port) for node in config.redis.startup_nodes] == [
        ("redis-1", 7000),
        ("redis-2", 7001),
    ]
