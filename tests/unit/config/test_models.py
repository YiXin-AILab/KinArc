"""RuntimeConfig 与 AgentConfig 严格校验测试。"""

from pathlib import Path

import pytest
from agentsystem.common.models import OutputMode
from agentsystem.config.models import (
    AgentConfig,
    AgentMediaConfig,
    ClusterRedisConfig,
    LocalAssetStoreConfig,
    MediaArchiveRuleConfig,
    RuntimeConfig,
    RuntimeLoggingConfig,
    RuntimeTraceConfig,
    S3AssetStoreConfig,
    StandaloneRedisConfig,
)
from agentsystem.llm.models import LLMGenerationConfig, MediaTransport
from pydantic import ValidationError


def runtime_data() -> dict[str, object]:
    """返回一份最小合法 Runtime 配置。"""
    return {
        "redis": {
            "type": "standalone",
            "host": "localhost",
            "port": 6379,
            "database": 0,
        },
        "managers": {
            "message": {"type": "redis"},
            "state": {"type": "redis"},
        },
        "dispatcher": {
            "ready_block_ms": 500,
            "resume_requeue_delay_ms": 50,
        },
        "asset": {"max_file_bytes": 104_857_600},
        "asset_store": {"type": "local"},
    }


def test_scheduler_does_not_enable_background_work_by_default() -> None:
    """未明确启用时不得自动运行后台任务。"""
    runtime = RuntimeConfig.model_validate(runtime_data())

    assert runtime.scheduler.enabled is False


def test_scheduler_rejects_invalid_timezone_and_unknown_fields() -> None:
    """时区和 Scheduler 字段必须在启动期严格校验。"""
    data = runtime_data()
    data["scheduler"] = {"timezone": "Mars/Olympus", "unexpected": True}

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_mysql_scheduler_requires_and_allows_database() -> None:
    """仅 Scheduler 使用 MySQL 时也必须共享 Application Engine。"""
    data = runtime_data()
    data["scheduler"] = {"enabled": True, "store": {"type": "mysql"}}
    with pytest.raises(ValidationError, match="database"):
        RuntimeConfig.model_validate(data)

    data["database"] = {
        "host": "localhost",
        "port": 3306,
        "username": "runtime",
        "password": "secret",
        "name": "runtime",
    }
    runtime = RuntimeConfig.model_validate(data)
    assert runtime.database is not None


@pytest.mark.parametrize(
    "directory",
    (Path("/tmp/logs"), Path("../logs"), Path("logs/../outside")),
)
def test_runtime_logging_config_rejects_unsafe_directory(directory: Path) -> None:
    """日志目录不能是绝对路径或包含父目录逃逸。"""
    with pytest.raises(ValidationError, match="directory"):
        RuntimeLoggingConfig(directory=directory)


@pytest.mark.parametrize("filename", ("logs/app.log", "../app.log", "."))
def test_runtime_logging_config_rejects_non_filename(filename: str) -> None:
    """活动文件名不能携带目录或代表特殊路径。"""
    with pytest.raises(ValidationError, match="filename"):
        RuntimeLoggingConfig(filename=filename)


def test_runtime_logging_config_requires_an_output() -> None:
    """完全无输出会静默丢失服务诊断，因此必须启动失败。"""
    with pytest.raises(ValidationError, match="console_enabled"):
        RuntimeLoggingConfig(console_enabled=False, file_enabled=False)


@pytest.mark.parametrize(
    "field_name",
    ("max_bytes", "retention_days", "max_backup_files"),
)
def test_runtime_logging_config_rejects_non_positive_rotation_limit(
    field_name: str,
) -> None:
    """滚动和保留边界必须为有限正数。"""
    with pytest.raises(ValidationError, match=field_name):
        RuntimeLoggingConfig.model_validate({field_name: 0})


def test_runtime_projector_timeout_must_be_positive() -> None:
    data = runtime_data()
    data["projector_timeout_seconds"] = 0

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_runtime_rejects_removed_noncritical_projector_timeout() -> None:
    data = runtime_data()
    data["noncritical_projector_timeout_seconds"] = 1.5

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_runtime_trace_config_validates_exporter_and_hides_headers() -> None:
    """Trace 配置必须保留通用 exporter 参数并隐藏认证 header。"""
    config = RuntimeTraceConfig(
        enabled=True,
        capture_content=True,
        service_name="agentsystem-test",
        endpoint="http://127.0.0.1:4318/v1/traces",
        headers={
            "Authorization": "Basic private-token",
            "x-scope-orgid": "tenant-a",
        },
        timeout_seconds=3.5,
        batch_schedule_delay_ms=250,
    )

    assert config.enabled is True
    assert config.capture_content is True
    assert config.service_name == "agentsystem-test"
    assert config.endpoint == "http://127.0.0.1:4318/v1/traces"
    assert config.headers["Authorization"].get_secret_value() == ("Basic private-token")
    assert config.headers["x-scope-orgid"].get_secret_value() == "tenant-a"
    assert config.timeout_seconds == 3.5
    assert config.batch_schedule_delay_ms == 250
    assert "private-token" not in repr(config)


def test_runtime_trace_config_rejects_removed_content_limit() -> None:
    """已移除的内容截断配置不得被静默接受。"""
    with pytest.raises(ValidationError, match="content_max_chars"):
        RuntimeTraceConfig.model_validate({"content_max_chars": 20_000})


def test_runtime_trace_config_requires_endpoint_when_enabled() -> None:
    """启用 Trace 时必须在启动期得到明确 OTLP endpoint。"""
    with pytest.raises(ValidationError, match="endpoint"):
        RuntimeTraceConfig(enabled=True)


@pytest.mark.parametrize(
    "endpoint",
    (
        "grpc://collector:4317",
        "http://user:password@collector:4318/v1/traces",
    ),
)
def test_runtime_trace_config_rejects_unsafe_endpoint(endpoint: str) -> None:
    """OTLP/HTTP endpoint 必须是无 user-info 的 HTTP/HTTPS URL。"""
    with pytest.raises(ValidationError, match="HTTP/HTTPS"):
        RuntimeTraceConfig(enabled=True, endpoint=endpoint)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("timeout_seconds", 0),
        ("batch_schedule_delay_ms", 0),
    ),
)
def test_runtime_trace_config_rejects_non_positive_export_limits(
    field_name: str,
    value: int,
) -> None:
    """Exporter timeout 和 batch delay 必须保持有限正值。"""
    with pytest.raises(ValidationError, match="greater than 0"):
        RuntimeTraceConfig.model_validate({field_name: value})


def agent_data() -> dict[str, object]:
    """返回一份最小合法 Agent 配置。"""
    return {
        "agent_id": "agent-1",
        "agent_name": "Example Agent",
        "prompt": "application/example/agent_config/prompt/main_prompt.yaml",
        "enabled_skills": ["stock-analysis"],
        "output_mode": "complete",
        "outbound": {
            "type": "log",
        },
        "llm_model": {
            "type": "litellm",
            "model": "openai/gpt-5-mini",
            "api_key": "top-secret-key",
        },
    }


def test_enabled_skills_preserves_order() -> None:
    """新配置名称保持 Skill 选择顺序。"""
    data = agent_data()
    data["enabled_skills"] = ["second-skill", "first-skill"]
    config = AgentConfig.model_validate(data)
    assert config.enabled_skills == ("second-skill", "first-skill")


@pytest.mark.parametrize("value", (None, ["demo", "demo"], [""]))
def test_enabled_skills_rejects_invalid_values(value: object) -> None:
    """更名后保留 null、重复名称及空名称校验。"""
    data = agent_data()
    data["enabled_skills"] = value
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_enabled_skills_defaults_to_empty() -> None:
    """省略或显式空列表都不启用 Skill。"""
    data = agent_data()
    del data["enabled_skills"]
    assert AgentConfig.model_validate(data).enabled_skills == ()
    data["enabled_skills"] = []
    assert AgentConfig.model_validate(data).enabled_skills == ()


@pytest.mark.parametrize("legacy_key", ("active_skills", "activate_skills"))
def test_rejects_legacy_skill_config_keys(legacy_key: str) -> None:
    """旧字段不作为配置兼容入口。"""
    data = agent_data()
    data[legacy_key] = ["stock-analysis"]
    with pytest.raises(ValidationError, match=legacy_key):
        AgentConfig.model_validate(data)


def test_agent_config_is_strict_but_not_frozen() -> None:
    """启动配置校验后仍可由装配层显式调整。"""
    config = AgentConfig.model_validate(agent_data())

    config.agent_name = "Renamed Agent"

    assert config.agent_name == "Renamed Agent"


def test_knowledge_bases_defaults_to_empty_and_parses_refs() -> None:
    """knowledge_bases 缺省为空，引用可携带检索覆盖。"""
    assert AgentConfig.model_validate(agent_data()).knowledge_bases == ()

    data = agent_data()
    data["knowledge_bases"] = [
        {"name": "pricing"},
        {"name": "tech_faq", "retrieval": {"strategy": "bm25", "top_k": 10}},
    ]

    config = AgentConfig.model_validate(data)

    assert [ref.name for ref in config.knowledge_bases] == ["pricing", "tech_faq"]
    override = config.knowledge_bases[1].retrieval
    assert override is not None and override.top_k == 10


def test_knowledge_bases_rejects_duplicates_and_unknown_fields() -> None:
    """知识库引用名必须唯一，引用内未知字段一律拒绝。"""
    data = agent_data()
    data["knowledge_bases"] = [{"name": "pricing"}, {"name": "pricing"}]
    with pytest.raises(ValidationError, match="knowledge_bases"):
        AgentConfig.model_validate(data)

    data = agent_data()
    data["knowledge_bases"] = [{"name": "pricing", "unknown_field": True}]
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_agent_plugin_options_preserve_custom_keys_and_json_values() -> None:
    """核心只保留 Plugin 自定义 key 及其 JSON 参数。"""
    data = agent_data()
    data["plugin_options"] = {
        "profile": {"namespace": "customer"},
        "shared": {"limit": 10},
    }

    config = AgentConfig.model_validate(data)

    assert config.plugin_options == {
        "profile": {"namespace": "customer"},
        "shared": {"limit": 10},
    }


def test_agent_capability_lists_have_explicit_defaults() -> None:
    """缺省配置保持现有 Tool/Middleware 行为且不启用 Provider。"""
    config = AgentConfig.model_validate(agent_data())

    assert config.enabled_tools is None
    assert config.enabled_middlewares is None
    assert config.enabled_context_providers == ()


def test_agent_plugin_options_reject_empty_key() -> None:
    """Plugin options 一级 key 必须非空。"""
    data = agent_data()
    data["plugin_options"] = {"": {}}

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_agent_capability_lists_preserve_patterns_and_provider_order() -> None:
    """Tool/Middleware 保留模式，Provider 保留精确声明顺序。"""
    data = agent_data()
    data["enabled_tools"] = ["workspace_*", "web_search"]
    data["enabled_middlewares"] = ["*-reminder"]
    data["enabled_context_providers"] = ["profile", "current-datetime"]

    config = AgentConfig.model_validate(data)

    assert config.enabled_tools == ("workspace_*", "web_search")
    assert config.enabled_middlewares == ("*-reminder",)
    assert config.enabled_context_providers == ("profile", "current-datetime")


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("enabled_tools", ["tool-*", "tool-*"]),
        ("enabled_middlewares", ["reminder", "reminder"]),
        ("enabled_context_providers", ["profile", "profile"]),
    ],
)
def test_agent_rejects_duplicate_capability_entries(
    field: str,
    values: list[str],
) -> None:
    """同一启用列表内的重复项必须在启动期失败。"""
    data = agent_data()
    data[field] = values

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


@pytest.mark.parametrize("field", ["enabled_tools", "enabled_middlewares"])
@pytest.mark.parametrize("pattern", ["bad?", "bad[", "bad]", "bad\\name"])
def test_agent_rejects_unsupported_capability_patterns(
    field: str,
    pattern: str,
) -> None:
    """Tool/Middleware 模式只允许星号具有通配语义。"""
    data = agent_data()
    data[field] = [pattern]

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


@pytest.mark.parametrize("field", ["enabled_tools", "enabled_middlewares"])
def test_agent_rejects_empty_capability_pattern(field: str) -> None:
    """Tool/Middleware 列表不能包含空名称。"""
    data = agent_data()
    data[field] = [""]

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


@pytest.mark.parametrize("field", ["enabled_tools", "enabled_middlewares"])
def test_agent_capability_pattern_lists_accept_null(field: str) -> None:
    """Tool/Middleware 的 null 保持全部启用语义。"""
    data = agent_data()
    data[field] = None

    assert getattr(AgentConfig.model_validate(data), field) is None


@pytest.mark.parametrize(
    "values",
    [None, [""], [{"type": "profile"}]],
)
def test_agent_rejects_invalid_context_provider_list(values: object) -> None:
    """Provider 列表拒绝 null、空名称和旧对象结构。"""
    data = agent_data()
    data["enabled_context_providers"] = values

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_agent_rejects_removed_context_providers_field() -> None:
    """旧 Provider 字段不得作为兼容入口继续存在。"""
    data = agent_data()
    data["context_providers"] = ["current-datetime"]

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_runtime_rejects_removed_dispatcher_concurrency() -> None:
    """旧并发槽位配置必须启动失败，避免被静默忽略。"""
    data = runtime_data()
    data["dispatcher"] = {"concurrency": 4}

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_media_archive_rules_default_empty() -> None:
    """Agent 未显式选择时不得自动归档媒体。"""
    assert AgentMediaConfig().archive_rules == ()


def test_asset_id_context_projection_defaults_off_and_accepts_boolean() -> None:
    """资产句柄只在 Agent 显式启用时进入模型上下文。"""
    assert AgentMediaConfig().include_asset_id_in_context is False
    assert (
        AgentMediaConfig.model_validate(
            {"include_asset_id_in_context": True}
        ).include_asset_id_in_context
        is True
    )


def test_asset_id_context_projection_rejects_non_boolean() -> None:
    """严格配置不得把字符串静默转换为布尔值。"""
    with pytest.raises(ValidationError):
        AgentMediaConfig.model_validate({"include_asset_id_in_context": "true"})


def test_media_transport_defaults_to_auto_and_accepts_only_global_modes() -> None:
    """未知值或按 Block 配置会让 adapter 行为无法确定。"""
    assert AgentMediaConfig().llm_transport is MediaTransport.AUTO
    assert (
        AgentMediaConfig.model_validate({"llm_transport": "base64"}).llm_transport
        is MediaTransport.BASE64
    )
    with pytest.raises(ValidationError):
        AgentMediaConfig.model_validate({"llm_transport": "url"})
    with pytest.raises(ValidationError):
        AgentMediaConfig.model_validate({"llm_transport": {"image": "base64"}})


def test_archive_rule_rejects_empty_or_duplicate_values() -> None:
    """归档规则两侧均须非空且不能含重复条件。"""
    with pytest.raises(ValidationError):
        MediaArchiveRuleConfig(block_types=(), source_types=("url",))
    with pytest.raises(ValidationError):
        MediaArchiveRuleConfig(
            block_types=("image", "image"),
            source_types=("base64",),
        )


def test_archive_rule_uses_only_supported_values() -> None:
    """Agent 规则只接受四类媒体与可归档的外部 Source。"""
    data = agent_data()
    data["media"] = {
        "archive_rules": [
            {
                "block_types": ["image", "document"],
                "source_types": ["url", "base64"],
            }
        ]
    }

    config = AgentConfig.model_validate(data)

    assert config.media.archive_rules[0].source_types == ("url", "base64")
    data["media"] = {
        "archive_rules": [{"block_types": ["image"], "source_types": ["asset"]}]
    }
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_local_asset_root_must_be_relative_and_contained() -> None:
    """Local store 路径不能绝对定位或通过父目录逃逸。"""
    with pytest.raises(ValidationError):
        LocalAssetStoreConfig(root=Path("/tmp/assets"))
    with pytest.raises(ValidationError):
        LocalAssetStoreConfig(root=Path("../assets"))

    assert LocalAssetStoreConfig().root == Path("data/assets")


def test_runtime_requires_asset_limit_and_store_selection() -> None:
    """Runtime 启动必须显式声明大小门禁和唯一 Store。"""
    data = runtime_data()
    del data["asset"]
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)

    data = runtime_data()
    del data["asset_store"]
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


@pytest.mark.parametrize(
    "asset",
    [
        {"max_file_bytes": 0},
        {"max_file_bytes": 1, "download_timeout_seconds": 0},
        {"max_file_bytes": 1, "upload_timeout_seconds": -1},
        {"max_file_bytes": 1, "allowed_url_schemes": []},
        {"max_file_bytes": 1, "allowed_url_schemes": ["https", "https"]},
        {"max_file_bytes": 1, "allowed_url_schemes": ["ftp"]},
        {"max_file_bytes": 1, "unknown": True},
    ],
)
def test_runtime_rejects_invalid_asset_limits(asset: dict[str, object]) -> None:
    """文件限制、URL scheme 和未知字段必须在启动期失败。"""
    data = runtime_data()
    data["asset"] = asset

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_runtime_rejects_removed_context_token_config() -> None:
    """已删除的 Runtime token 估算配置必须按未知字段拒绝。"""
    data = runtime_data()
    data["context"] = {"default_media_tokens": 1_024}

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_agent_compaction_config_accepts_explicit_overrides() -> None:
    """Agent 的显式压缩参数应传递到配置模型。"""
    data = agent_data()
    data["compaction"] = {
        "safety_margin_tokens": 1_024,
        "trigger_ratio": 0.75,
        "recent_message_ratio": 0.15,
        "media_block_tokens": 4_096,
        "tool_argument_max_chars": 100,
        "tool_result_max_chars": 200,
    }

    config = AgentConfig.model_validate(data)

    assert config.compaction.safety_margin_tokens == 1_024
    assert config.compaction.recent_message_ratio == 0.15
    assert config.compaction.media_block_tokens == 4_096


@pytest.mark.parametrize(
    "value",
    [
        {"recent_message_ratio": 0.8, "trigger_ratio": 0.8},
        {"safety_margin_tokens": 0},
        {"media_block_tokens": 0},
        {"tool_argument_max_chars": 0},
        {"target_ratio": 0.5},
        {"keep_recent_conversation_turns": 3},
        {"keep_recent_tool_rounds": 2},
        {"retry_count": 1},
        {"unknown": True},
    ],
)
def test_agent_rejects_invalid_compaction(value: dict[str, object]) -> None:
    """比例、窗口、字符上限和未知字段必须在启动期失败。"""
    data = agent_data()
    data["compaction"] = value

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_s3_asset_secrets_do_not_leak() -> None:
    """S3/OSS 凭证必须使用 SecretStr 并从 repr 隐藏。"""
    config = S3AssetStoreConfig(
        endpoint_url="https://oss.example.test",
        bucket="assets",
        region="cn-test-1",
        access_key="access-test-secret",
        secret_key="secret-test-secret",
    )

    assert config.access_key.get_secret_value() == "access-test-secret"
    assert "access-test-secret" not in repr(config)
    assert "secret-test-secret" not in repr(config)


def test_runtime_config_selects_standalone_redis() -> None:
    """Standalone 显式配置应选择对应模型并保留超时默认值。"""
    config = RuntimeConfig.model_validate(runtime_data())

    assert isinstance(config.redis, StandaloneRedisConfig)
    assert config.redis.host == "localhost"
    assert config.redis.port == 6379
    assert config.redis.database == 0
    assert config.redis.socket_timeout_seconds == 5.0


def test_runtime_config_selects_cluster_redis() -> None:
    """Cluster 节点配置应选择对应模型并忽略空白凭证。"""
    data = runtime_data()
    data["redis"] = {
        "type": "cluster",
        "startup_nodes": [
            {"host": "redis-1", "port": 7000},
            {"host": "redis-2", "port": 7001},
        ],
        "username": "",
        "password": "",
    }

    config = RuntimeConfig.model_validate(data)

    assert isinstance(config.redis, ClusterRedisConfig)
    assert config.redis.username is None
    assert config.redis.password is None


@pytest.mark.parametrize(
    "redis",
    [
        {"host": "localhost", "port": 6379},
        {"url": "redis://localhost:6379/0"},
        {
            "type": "standalone",
            "host": "localhost",
            "startup_nodes": [
                {"host": "redis-1", "port": 7000},
                {"host": "redis-2", "port": 7001},
            ],
        },
        {
            "type": "cluster",
            "host": "localhost",
            "port": 6379,
            "database": 0,
            "startup_nodes": [
                {"host": "redis-1", "port": 7000},
                {"host": "redis-2", "port": 7001},
            ],
        },
        {"type": "cluster", "startup_nodes": []},
        {
            "type": "cluster",
            "startup_nodes": [{"host": "redis-1", "port": 7000}],
        },
        {
            "type": "cluster",
            "startup_nodes": [
                {"host": "Redis-1", "port": 7000},
                {"host": "redis-1", "port": 7000},
            ],
        },
        {"type": "standalone", "host": "localhost", "port": 0},
        {"type": "standalone", "host": "localhost", "port": 65_536},
        {"type": "standalone", "host": "localhost", "database": -1},
        {
            "type": "cluster",
            "startup_nodes": [
                {"host": "redis-1", "port": 0},
                {"host": "redis-2", "port": 7001},
            ],
        },
    ],
)
def test_runtime_rejects_invalid_redis_config(
    redis: dict[str, object],
) -> None:
    """模式缺失、跨模式字段和非法节点应在启动时失败。"""
    data = runtime_data()
    data["redis"] = redis

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_runtime_redis_validation_error_hides_password() -> None:
    """Redis 配置失败不能在 ValidationError 中回显密码。"""
    data = runtime_data()
    data["redis"] = {
        "type": "cluster",
        "startup_nodes": [{"host": "redis-1", "port": 7000}],
        "password": "super-secret-test-value",
    }

    with pytest.raises(ValidationError) as captured:
        RuntimeConfig.model_validate(data)

    assert "super-secret-test-value" not in str(captured.value)


@pytest.mark.parametrize(
    ("message_type", "state_type"),
    [
        ("sql", "redis"),
        ("redis", "sql"),
        ("sql", "sql"),
    ],
)
def test_runtime_accepts_any_sql_manager_with_one_database(
    message_type: str,
    state_type: str,
) -> None:
    """任一 Manager 选择 SQL 时共用唯一 database 配置。"""
    data = runtime_data()
    data["database"] = {
        "type": "mysql",
        "host": "mysql",
        "name": "agentsystem",
        "username": "agentsystem",
        "password": "secret",
    }
    data["managers"] = {
        "message": {"type": message_type},
        "state": {"type": state_type},
    }

    config = RuntimeConfig.model_validate(data)

    assert config.managers.message.type == message_type
    assert config.managers.state.type == state_type
    assert config.database is not None
    assert config.database.name == "agentsystem"


@pytest.mark.parametrize(
    ("message_type", "state_type"),
    [("sql", "redis"), ("redis", "sql"), ("sql", "sql")],
)
def test_runtime_requires_database_for_any_sql_manager(
    message_type: str,
    state_type: str,
) -> None:
    data = runtime_data()
    data["managers"] = {
        "message": {"type": message_type},
        "state": {"type": state_type},
    }

    with pytest.raises(ValidationError, match="database"):
        RuntimeConfig.model_validate(data)


def test_runtime_rejects_database_when_both_managers_are_redis() -> None:
    data = runtime_data()
    data["database"] = {
        "type": "mysql",
        "host": "mysql",
        "name": "agentsystem",
    }

    with pytest.raises(ValidationError, match="database"):
        RuntimeConfig.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled_skills", [""]),
        ("unknown_field", True),
    ],
)
def test_agent_rejects_unsupported_or_unknown_values(
    field: str,
    value: object,
) -> None:
    """Agent 配置拒绝空 Skill 和未知字段。"""
    data = agent_data()
    data[field] = value

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_agent_accepts_stream_output_mode() -> None:
    """Agent 可显式选择 STREAM 输出。"""
    data = agent_data()
    data["output_mode"] = "stream"
    data["outbound"] = {"type": "stream"}

    agent = AgentConfig.model_validate(data)

    assert agent.output_mode is OutputMode.STREAM
    assert agent.outbound.type == "stream"
    assert agent.outbound.options == {}


@pytest.mark.parametrize(
    "public_stream",
    [
        {"terminal_grace_seconds": 86_400},
        {"max_tool_payload_bytes": 65_537},
        {"heartbeat_seconds": 0},
        {"unknown": True},
    ],
)
def test_runtime_rejects_invalid_public_stream_config(
    public_stream: dict[str, object],
) -> None:
    """保留期、payload、heartbeat 和未知字段必须在启动期失败。"""
    data = runtime_data()
    data["public_stream"] = public_stream

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_api_key_is_secret_and_repr_does_not_leak() -> None:
    """模型密钥必须以 SecretStr 保存且不会出现在 repr。"""
    config = AgentConfig.model_validate(agent_data())

    api_key = config.llm_model.api_key
    assert api_key is not None
    assert api_key.get_secret_value() == "top-secret-key"
    assert "top-secret-key" not in repr(config)


def test_agent_uses_outbound_and_rejects_legacy_output() -> None:
    """Agent 只接受语义明确的 outbound，不兼容旧 output 字段。"""
    data = agent_data()

    config = AgentConfig.model_validate(data)

    assert config.outbound.type == "log"
    assert config.outbound.options == {}
    data["output"] = data.pop("outbound")
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_agent_outbound_type_is_independent_from_output_mode() -> None:
    """生成模式和输出器类型必须允许独立组合。"""
    stream_to_log = agent_data()
    stream_to_log["output_mode"] = "stream"
    complete_to_stream = agent_data()
    complete_to_stream["outbound"] = {"type": "stream"}

    first = AgentConfig.model_validate(stream_to_log)
    second = AgentConfig.model_validate(complete_to_stream)

    assert first.output_mode is OutputMode.STREAM
    assert first.outbound.type == "log"
    assert second.output_mode is OutputMode.COMPLETE
    assert second.outbound.type == "stream"


def test_agent_model_config_carries_provider_and_context_policy() -> None:
    """模型参数与压缩策略分别承载输出上限和本地预算。"""
    data = agent_data()
    data["llm_model"] = {
        "type": "litellm",
        "model": "openai/deepseek-v4-flash",
        "max_output_tokens": 16_384,
        "parallel_tool_calls": True,
        "provider_options": {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        },
        "context_window": 64_000,
    }
    data["compaction"] = {
        "safety_margin_tokens": 1_024,
        "trigger_ratio": 0.80,
        "recent_message_ratio": 0.20,
        "media_block_tokens": 4_096,
    }

    config = AgentConfig.model_validate(data)

    assert config.llm_model.max_output_tokens == 16_384
    assert config.compaction.safety_margin_tokens == 1_024
    assert config.compaction.media_block_tokens == 4_096
    assert config.llm_model.parallel_tool_calls is True
    assert config.llm_model.provider_options["reasoning_effort"] == "high"


def test_agent_model_config_rejects_exhausted_context_budget() -> None:
    """最大输出不能耗尽模型上下文窗口。"""
    data = agent_data()
    data["llm_model"] = {
        "type": "litellm",
        "model": "openai/deepseek-v4-flash",
        "max_output_tokens": 4_096,
        "context_window": 4_096,
    }

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_agent_model_projects_provider_free_generation_config() -> None:
    """模型请求只能接收生成字段，不能携带凭证或上下文预算。"""
    data = agent_data()
    data["llm_model"] = {
        "model": "openai/test-projection",
        "api_key": "test-private-key",
        "max_output_tokens": 1234,
    }
    config = AgentConfig.model_validate(data).llm_model

    generation = config.to_generation_config()

    assert type(generation) is LLMGenerationConfig
    assert generation.model == "openai/test-projection"
    assert generation.max_output_tokens == 1234
    assert "api_key" not in generation.model_dump()
    assert "context_window" not in generation.model_dump()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_provider", {"type": "litellm"}),
        ("generation", {"model": "openai/legacy"}),
        ("max_tokens", 16_384),
    ],
)
def test_llm_model_rejects_removed_nested_or_legacy_fields(
    field: str,
    value: object,
) -> None:
    """旧嵌套和旧字段必须失败，避免形成第二配置权威。"""
    data = agent_data()
    target = data["llm_model"]
    assert isinstance(target, dict)
    target[field] = value

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


@pytest.mark.parametrize(
    "field",
    ["reserved_output_tokens", "safety_margin_tokens"],
)
def test_llm_model_rejects_removed_budget_fields(field: str) -> None:
    """模型配置不再接受压缩侧预算字段。"""
    data = agent_data()
    llm_model = data["llm_model"]
    assert isinstance(llm_model, dict)
    llm_model[field] = 2_048

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(data)


def test_compaction_rejects_recent_ratio_at_or_above_trigger() -> None:
    """近端软预算必须严格低于压缩触发比例。"""
    data = agent_data()
    data["compaction"] = {
        "trigger_ratio": 0.80,
        "recent_message_ratio": 0.80,
    }

    with pytest.raises(ValidationError, match="recent_message_ratio"):
        AgentConfig.model_validate(data)


def test_runtime_rejects_removed_vendor_trace_configuration() -> None:
    """Trace 配置拒绝供应商 discriminator 和专用凭证字段。"""
    data = runtime_data()
    data["trace"] = {
        "type": "langfuse",
        "base_url": "http://127.0.0.1:3000",
        "public_key": "pk-test-secret",
        "secret_key": "sk-test-secret",
        "service_name": "agentsystem-test",
    }

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)
