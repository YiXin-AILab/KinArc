"""Runtime 与 Agent 的启动期配置模型。"""

from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from agentsystem.common.models import JsonValue, OutputMode, StrictModel
from agentsystem.knowledge.models import KnowledgeBaseRef
from agentsystem.llm.models import LLMGenerationConfig, MediaTransport
from agentsystem.memory.store import MAX_CONTENT_CHARS, MemoryScope

type NonEmptyName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class RedisConnectionConfig(StrictModel):
    """Standalone 与 Cluster 共用的启动期连接参数，不创建连接。

    Attributes:
        username: 可选 ACL 用户名，空白字符串视为未配置。
        password: 可选密码，以 SecretStr 隐藏常规表示中的正文。
        key_prefix: 模块 Redis key 使用的应用命名空间。
        socket_timeout_seconds: 建连超时秒数，并作为普通命令超时余量基线；
            共享 client 的实际命令超时还包含最长阻塞读取窗口。
    """

    model_config = ConfigDict(
        hide_input_in_errors=True,
    )

    username: NonEmptyName | None = None
    password: SecretStr | None = None
    key_prefix: NonEmptyName = "agent"
    socket_timeout_seconds: float = Field(default=5.0, gt=0)

    @field_validator("username", "password", mode="before")
    @classmethod
    def normalize_blank_credentials(cls, value: object) -> object:
        """将空白凭证视为未配置，同时保留非空密码的原始内容。

        Args:
            value: 字段类型校验前的原始凭证。

        Returns:
            空白字符串对应 None，其余值原样进入后续字段校验。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class StandaloneRedisConfig(RedisConnectionConfig):
    """供 Application 创建单节点 Redis client 的配置。

    Attributes:
        type: 固定为 standalone 的联合类型判别值。
        host: Redis 主机名或地址。
        port: Redis 服务端口。
        database: 非负逻辑数据库编号。
    """

    type: Literal["standalone"]
    host: NonEmptyName
    port: int = Field(default=6379, ge=1, le=65_535)
    database: int = Field(default=0, ge=0)


class RedisClusterNode(StrictModel):
    """用于发现原生 Redis Cluster 拓扑的启动节点，不代表完整集群拓扑。

    Attributes:
        host: 启动节点主机名或地址。
        port: 启动节点服务端口。
    """

    host: NonEmptyName
    port: int = Field(default=6379, ge=1, le=65_535)


class ClusterRedisConfig(RedisConnectionConfig):
    """供 Application 创建原生 Redis Cluster client 的配置。

    所有启动节点共用继承的凭证；本模型只检查节点配置，不连接或探测拓扑。

    Attributes:
        type: 固定为 cluster 的判别值。
        startup_nodes: 至少两个不同的 host/port 组合，host 比较忽略大小写。
    """

    type: Literal["cluster"]
    startup_nodes: tuple[RedisClusterNode, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_unique_nodes(self) -> Self:
        """校验启动节点唯一性，host 比较忽略大小写。

        Returns:
            当前已通过校验的配置实例。

        Raises:
            ValueError: 存在重复 host/port 组合。
        """
        normalized_nodes = {
            (node.host.casefold(), node.port) for node in self.startup_nodes
        }
        if len(normalized_nodes) != len(self.startup_nodes):
            raise ValueError("startup_nodes 包含重复节点")
        return self


type RuntimeRedisConfig = Annotated[
    StandaloneRedisConfig | ClusterRedisConfig,
    Field(discriminator="type"),
]


class MysqlDatabaseConfig(StrictModel):
    """供 Application 创建共享 MySQL AsyncEngine 的参数，不拥有连接池。

    Attributes:
        type: 固定为 mysql 的判别值。
        host: 数据库主机名或地址。
        port: 数据库服务端口。
        name: 数据库名称。
        username: 可选用户名，空白字符串视为未配置。
        password: 可选 SecretStr 密码。
        connect_timeout_seconds: 建立连接的等待上限。
        pool_size: 连接池常驻容量。
        max_overflow: 超出常驻容量可临时创建的连接数量。
        pool_timeout_seconds: 等待池连接的期限。
        operation_timeout_seconds: 数据库操作的外层期限；时间单位均为秒。
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    type: Literal["mysql"] = "mysql"
    host: NonEmptyName
    port: int = Field(default=3306, ge=1, le=65_535)
    name: NonEmptyName
    username: NonEmptyName | None = None
    password: SecretStr | None = None
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    pool_size: int = Field(default=10, gt=0)
    max_overflow: int = Field(default=10, ge=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    operation_timeout_seconds: float = Field(default=10.0, gt=0)

    @field_validator("username", "password", mode="before")
    @classmethod
    def normalize_blank_credentials(cls, value: object) -> object:
        """将空白数据库凭证视为未配置。

        Args:
            value: 字段类型校验前的原始凭证。

        Returns:
            空白字符串对应 None；非空密码不裁剪、不改写。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class RedisManagerAdapterConfig(StrictModel):
    """选择 Redis Manager 实现的配置标记，不创建或持有 client。

    Attributes:
        type: 固定为 redis，使用 Runtime 共享 Redis 基础设施。
    """

    type: Literal["redis"] = "redis"


class SqlManagerAdapterConfig(StrictModel):
    """选择 SQL Manager 实现的配置标记，不创建或持有 Engine。

    Attributes:
        type: 固定为 sql，使用 Runtime 共享数据库基础设施。
    """

    type: Literal["sql"] = "sql"


type ManagerAdapterConfig = Annotated[
    RedisManagerAdapterConfig | SqlManagerAdapterConfig,
    Field(discriminator="type"),
]


class ManagerAdaptersConfig(StrictModel):
    """分别确定 MessageManager 与 StateManager 的唯一权威实现。

    两者可独立选择，选择 SQL 不表示向 Redis 双写。

    Attributes:
        message: 模型上下文消息的存储实现。
        state: 执行控制状态的存储实现。
    """

    message: ManagerAdapterConfig = Field(default_factory=RedisManagerAdapterConfig)
    state: ManagerAdapterConfig = Field(default_factory=RedisManagerAdapterConfig)


class DispatcherConfig(StrictModel):
    """调度器读取、锁续租与恢复重投参数，由宿主用于组装执行任务。

    Attributes:
        ready_block_ms: Ready 读取阻塞毫秒数。
        lock_ttl_seconds: 会话锁租约秒数。
        heartbeat_interval_seconds: 两次锁心跳之间的秒数。
        resume_requeue_delay_ms: 恢复消息锁忙后重新入队的延迟毫秒数。
    """

    ready_block_ms: int = Field(default=1_000, gt=0)
    lock_ttl_seconds: float = Field(default=30.0, gt=0)
    heartbeat_interval_seconds: float = Field(default=10.0, gt=0)
    resume_requeue_delay_ms: int = Field(default=100, ge=0)


class LocalScheduleManagerConfig(StrictModel):
    """选择进程内 ScheduleManager，不提供跨进程共享或持久化。

    Attributes:
        type: 固定为 local 的存储选择标记。
    """

    type: Literal["local"] = "local"


class RedisScheduleManagerConfig(StrictModel):
    """选择基于 Runtime 共享 Redis 的 ScheduleManager。

    Attributes:
        type: 固定为 redis 的存储选择标记。
    """

    type: Literal["redis"] = "redis"


class SqlScheduleManagerConfig(StrictModel):
    """选择基于 Runtime 共享数据库的 ScheduleManager。

    Attributes:
        type: 固定为 mysql 的存储选择标记。
    """

    type: Literal["mysql"] = "mysql"


type ScheduleManagerConfig = Annotated[
    LocalScheduleManagerConfig | RedisScheduleManagerConfig | SqlScheduleManagerConfig,
    Field(discriminator="type"),
]


class SchedulerConfig(StrictModel):
    """启动期定时调度配置，不启动扫描任务或加载时区以外的外部资源。

    Attributes:
        enabled: 是否启动调度扫描。
        timezone: 已验证的 IANA 默认时区。
        scan_interval_seconds: 两次扫描之间的秒数。
        scan_batch_size: 一次扫描的任务数量上限。
        store: 唯一的调度任务权威存储。
    """

    enabled: bool = False
    timezone: NonEmptyName = "Asia/Shanghai"
    scan_interval_seconds: float = Field(default=1.0, gt=0)
    scan_batch_size: int = Field(default=100, ge=1, le=10_000)
    store: ScheduleManagerConfig = Field(default_factory=LocalScheduleManagerConfig)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """在启动期验证 IANA 时区名称。

        Args:
            value: 配置的时区名称。

        Returns:
            可由 ZoneInfo 加载的原始名称。

        Raises:
            ValueError: 当前环境中无法加载该时区。
        """
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("scheduler.timezone 不是有效 IANA 时区") from error
        return value


class RuntimeLLMProviderConfig(StrictModel):
    """Runtime 级 LLM adapter 选择，不包含 Agent 的模型凭证。

    Attributes:
        type: 当前支持的 litellm adapter 标记。
    """

    type: Literal["litellm"] = "litellm"


class RuntimeLoggingConfig(StrictModel):
    """交给日志初始化入口的输出和归档配置，本模型不创建 Handler。

    Attributes:
        level: root 日志级别。
        console_enabled: 是否启用控制台输出。
        file_enabled: 是否启用文件输出，至少启用一个输出端。
        directory: 项目根目录下的相对日志目录。
        filename: 不带目录分隔符的活动日志文件名。
        max_bytes: 活动文件滚动的字节阈值。
        retention_days: 归档按本地日期保留的天数。
        max_backup_files: 保留归档数量上限。
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    console_enabled: bool = True
    file_enabled: bool = True
    directory: Path = Path("logs")
    filename: NonEmptyName = "agentsystem.log"
    max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    retention_days: int = Field(default=14, gt=0)
    max_backup_files: int = Field(default=100, gt=0)

    @field_validator("directory")
    @classmethod
    def validate_directory(cls, value: Path) -> Path:
        """校验日志目录是项目内的相对路径。

        Args:
            value: 已解析的目录路径。

        Returns:
            校验后的原始路径，不访问文件系统或解析符号链接。

        Raises:
            ValueError: 路径为绝对路径或包含父目录跳转。
        """
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("logging.directory 必须是项目内相对路径")
        return value

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        """校验活动日志名称不包含目录部分。

        Args:
            value: 已去除首尾空白的文件名。

        Returns:
            校验后的文件名。

        Raises:
            ValueError: 文件名为点目录或包含路径分隔符。
        """
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("logging.filename 只能是单个文件名")
        return value

    @model_validator(mode="after")
    def validate_output_enabled(self) -> Self:
        """验证至少启用一个日志输出端。

        Returns:
            当前配置实例。

        Raises:
            ValueError: 控制台和文件输出均关闭。
        """
        if not self.console_enabled and not self.file_enabled:
            raise ValueError("console_enabled 和 file_enabled 不能同时关闭")
        return self


class PublicStreamConfig(StrictModel):
    """公开 SSE 通道的可见性、存储与连接预算，由发布和读取组件消费。

    Attributes:
        enabled: 是否启用公开事件流。
        include_thinking: 是否公开思考内容。
        include_tool_arguments: 是否公开工具参数。
        include_tool_results: 是否公开工具结果。
        retention_seconds: 活动会话事件保留秒数。
        terminal_grace_seconds: 终态后的保留秒数，须小于普通保留期。
        max_events_per_session: 单会话事件数量上限。
        max_event_bytes: 单条公开事件的字节上限。
        max_tool_payload_bytes: 工具 payload 字节上限，不大于事件上限。
        read_batch_size: 单次读取批量大小。
        block_ms: 读取事件的阻塞毫秒数。
        heartbeat_seconds: SSE 心跳间隔秒数。
        max_connection_seconds: 单连接最长存活秒数。
        max_connections_total: 总连接数限制。
        max_connections_per_user: 单用户连接数限制。
        max_connections_per_session: 单会话连接数限制。
    """

    enabled: bool = True
    include_thinking: bool = True
    include_tool_arguments: bool = False
    include_tool_results: bool = False
    retention_seconds: int = Field(default=86_400, gt=0)
    terminal_grace_seconds: int = Field(default=300, gt=0)
    max_events_per_session: int = Field(default=50_000, gt=0)
    max_event_bytes: int = Field(default=65_536, gt=0)
    max_tool_payload_bytes: int = Field(default=16_384, gt=0)
    read_batch_size: int = Field(default=100, gt=0)
    block_ms: int = Field(default=10_000, gt=0)
    heartbeat_seconds: float = Field(default=15, gt=0)
    max_connection_seconds: float = Field(default=1_800, gt=0)
    max_connections_total: int = Field(default=1_000, gt=0)
    max_connections_per_user: int = Field(default=10, gt=0)
    max_connections_per_session: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def validate_stream_limits(self) -> Self:
        """校验终态保留窗口和工具 payload 的组合边界。

        Returns:
            当前配置实例。

        Raises:
            ValueError: 终态保留期不小于普通保留期，或工具上限超过事件上限。
        """
        if self.terminal_grace_seconds >= self.retention_seconds:
            raise ValueError("terminal_grace_seconds 必须小于 retention_seconds")
        if self.max_tool_payload_bytes > self.max_event_bytes:
            raise ValueError("max_tool_payload_bytes 不能超过 max_event_bytes")
        return self


class RuntimeTraceConfig(StrictModel):
    """Trace 策略与 OTLP/HTTP 导出参数，不创建 exporter 或网络连接。

    Attributes:
        enabled: 是否启用 Trace 导出。
        capture_content: 是否采集内容正文。
        service_name: 遥测服务名称。
        endpoint: 无 user-info 的 HTTP/HTTPS 导出地址，启用时必填。
        headers: 导出请求头，其值使用 SecretStr 保存。
        timeout_seconds: 单次导出等待秒数。
        batch_schedule_delay_ms: 批量导出调度间隔毫秒数。
    """

    enabled: bool = False
    capture_content: bool = False
    service_name: NonEmptyName = "agentsystem"
    endpoint: str | None = None
    headers: dict[NonEmptyName, SecretStr] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, gt=0)
    batch_schedule_delay_ms: int = Field(default=500, gt=0)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        """校验可选 Trace 导出地址。

        Args:
            value: 显式 HTTP/HTTPS URL，或未配置时的 None。

        Returns:
            原始地址或 None，不执行网络探测。

        Raises:
            ValueError: URL 缺少主机、含 user-info 或 scheme/端口非法。
        """
        if value is not None:
            _validate_http_url(value, "endpoint")
        return value

    @model_validator(mode="after")
    def validate_enabled_endpoint(self) -> Self:
        """验证启用 Trace 时存在导出地址。

        Returns:
            当前配置实例。

        Raises:
            ValueError: 已启用 Trace 但 endpoint 为 None。
        """
        if self.enabled and self.endpoint is None:
            raise ValueError("启用 Trace 时 endpoint 必填")
        return self


class AssetLimitsConfig(StrictModel):
    """媒体获取与存储的统一边界配置，不执行下载或上传。

    Attributes:
        max_file_bytes: 单文件字节上限。
        download_timeout_seconds: 下载等待期限，单位秒。
        upload_timeout_seconds: 上传等待期限，单位秒。
        allowed_url_schemes: 允许的媒体 URL scheme，不允许重复。
    """

    max_file_bytes: int = Field(gt=0)
    download_timeout_seconds: float = Field(default=10.0, gt=0)
    upload_timeout_seconds: float = Field(default=30.0, gt=0)
    allowed_url_schemes: tuple[Literal["http", "https"], ...] = Field(
        default=("http", "https"),
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_unique_schemes(self) -> Self:
        """校验媒体 URL scheme 不重复。

        Returns:
            当前配置实例。

        Raises:
            ValueError: allowed_url_schemes 包含重复项。
        """
        if len(self.allowed_url_schemes) != len(set(self.allowed_url_schemes)):
            raise ValueError("allowed_url_schemes 包含重复项")
        return self


class LocalAssetStoreConfig(StrictModel):
    """供宿主创建本地 AssetStore 的路径配置，不创建目录。

    Attributes:
        type: 固定为 local 的判别值。
        root: 项目根目录内的相对存储路径，不允许父目录跳转。
    """

    type: Literal["local"] = "local"
    root: Path = Path("data/assets")

    @field_validator("root")
    @classmethod
    def validate_relative_root(cls, value: Path) -> Path:
        """校验本地资产根目录的相对路径结构。

        Args:
            value: 已解析的存储路径。

        Returns:
            原始相对路径，不创建目录或解析符号链接。

        Raises:
            ValueError: 路径为绝对路径或包含父目录跳转。
        """
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("Local AssetStore root 必须是项目内相对路径")
        return value


class S3AssetStoreConfig(StrictModel):
    """供宿主创建 S3/OSS 兼容存储的连接参数，不拥有 SDK client。

    Attributes:
        type: 固定为 s3 的判别值。
        endpoint_url: 无 user-info 的 HTTP/HTTPS 服务地址。
        bucket: 目标存储桶名称。
        region: SDK 使用的区域标识。
        access_key: 使用 SecretStr 保存的访问标识。
        secret_key: 使用 SecretStr 保存的访问密钥。
        multipart_part_bytes: 分片上传块大小，至少 5 MiB。
    """

    type: Literal["s3"] = "s3"
    endpoint_url: NonEmptyName
    bucket: NonEmptyName
    region: NonEmptyName
    access_key: SecretStr = Field(min_length=1)
    secret_key: SecretStr = Field(min_length=1)
    multipart_part_bytes: int = Field(default=8 * 1024 * 1024, ge=5 * 1024 * 1024)

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str) -> str:
        """校验 S3 兼容服务的基础 URL。

        Args:
            value: 已去除首尾空白的服务地址。

        Returns:
            原始地址，不执行网络探测。

        Raises:
            ValueError: URL 缺少主机、含 user-info 或 scheme/端口非法。
        """
        _validate_http_url(value, "endpoint_url")
        return value


class HydraAssetStoreConfig(StrictModel):
    """Hydra 附件平台存储参数，由具体 adapter 解释业务字段。

    本模型校验基础 URL 格式，不获取 token 或访问附件平台。

    Attributes:
        type: 固定为 hydra 的判别值。
        token_url: token 获取地址。
        upload_url: 附件上传地址。
        file_prefix: 用于生成附件地址的 HTTP/HTTPS 前缀。
        business_type: 平台业务类型。
        business_id: 平台业务标识。
        template_code: 平台模板编码。
        item_code: 平台条目编码。
        creator_id: 上传创建者标识。
        creator_name: 可选创建者名称。
    """

    type: Literal["hydra"] = "hydra"
    token_url: NonEmptyName
    upload_url: NonEmptyName
    file_prefix: NonEmptyName
    business_type: NonEmptyName
    business_id: NonEmptyName
    template_code: NonEmptyName
    item_code: NonEmptyName
    creator_id: NonEmptyName
    creator_name: NonEmptyName | None = None

    @field_validator("token_url", "upload_url", "file_prefix")
    @classmethod
    def validate_service_url(cls, value: str, info: object) -> str:
        """校验 Hydra 服务地址并保留具体字段的错误定位。

        Args:
            value: 已去除首尾空白的服务地址。
            info: Pydantic 校验上下文，仅使用其 field_name。

        Returns:
            原始地址，不执行网络探测。

        Raises:
            ValueError: URL 缺少主机、含 user-info 或 scheme/端口非法。
        """
        field_name = getattr(info, "field_name", "url")
        _validate_http_url(value, field_name)
        return value


type RuntimeAssetStoreConfig = Annotated[
    LocalAssetStoreConfig | S3AssetStoreConfig | HydraAssetStoreConfig,
    Field(discriminator="type"),
]


def _validate_http_url(value: str, field_name: str) -> None:
    """校验基础设施服务地址的 scheme、host 和 user-info。"""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field_name} 不是合法 URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise ValueError(f"{field_name} 必须是无 user-info 的 HTTP/HTTPS URL")


class MemoryStoreConfig(StrictModel):
    """选择唯一记忆权威存储，不另外配置连接。

    Attributes:
        type: redis、db 或 local 存储标记。
    """

    type: Literal["redis", "db", "local"] = "redis"


class RuntimeMemoryConfig(StrictModel):
    """记忆基础设施开关，由 Runtime 复用已有连接和存储生命周期。

    Attributes:
        enabled: 是否创建内置 Memory 服务。
        store: Memory 的唯一权威存储选择。
    """

    enabled: StrictBool = False
    store: MemoryStoreConfig = Field(default_factory=MemoryStoreConfig)


class MemoryMaxCharsConfig(StrictModel):
    """Memory 正文写入字符上限，读取和上下文注入不在此截断。

    Attributes:
        user_global: 用户全局记忆的字符额度。
        user_agent: 用户在当前 Agent 下记忆的字符额度。
    """

    user_global: int = Field(default=1000, strict=True, ge=1, le=MAX_CONTENT_CHARS)
    user_agent: int = Field(default=2000, strict=True, ge=1, le=MAX_CONTENT_CHARS)


class AgentMemoryConfig(StrictModel):
    """Agent 开放的记忆范围和写入额度，由 MemoryService 消费。

    Attributes:
        scopes: 启用的记忆作用域，保持配置顺序且不可重复。
        max_chars: 各作用域正文写入的字符额度。
    """

    scopes: tuple[MemoryScope, ...] = (MemoryScope.USER_GLOBAL, MemoryScope.USER_AGENT)
    max_chars: MemoryMaxCharsConfig = Field(default_factory=MemoryMaxCharsConfig)

    @field_validator("scopes")
    @classmethod
    def validate_scopes(
        cls, scopes: tuple[MemoryScope, ...]
    ) -> tuple[MemoryScope, ...]:
        """校验记忆作用域不重复，保留配置顺序。

        Args:
            scopes: 已解析的 MemoryScope 元组。

        Returns:
            原始作用域元组。

        Raises:
            ValueError: 存在重复作用域。
        """
        if len(scopes) != len(set(scopes)):
            raise ValueError("Memory scopes 包含重复项")
        return scopes


class TodoStoreConfig(StrictModel):
    """选择唯一 Todo 权威存储，不另建连接配置。

    Attributes:
        type: redis、db 或 local 存储标记。
    """

    type: Literal["redis", "db", "local"] = "redis"


class RuntimeTodoConfig(StrictModel):
    """Todo 共享存储配置；工具是否暴露由 Agent 启用列表决定。

    Attributes:
        store: Todo 的唯一权威存储选择。
    """

    store: TodoStoreConfig = Field(default_factory=TodoStoreConfig)


class RuntimeConfig(StrictModel):
    """Runtime 启动期的基础设施与执行边界配置，不直接创建资源。

    Application 依据该模型管理共享连接及组件生命周期。数据库配置须与
    实际选择数据库的 Manager、Scheduler、Memory 或 Todo 一致。

    Attributes:
        redis: 调度和 Redis adapter 共用的连接配置。
        database: SQL 组件共用的数据库配置，未选择数据库时必须省略。
        asset: 媒体获取和上传限制。
        asset_store: 媒体存储实现及其参数。
        managers: Message 和 State 的独立权威存储选择。
        dispatcher: Ready 调度和会话锁参数。
        scheduler: 定时扫描和存储策略。
        memory: 内置记忆基础设施开关及存储。
        todo: Todo 权威存储。
        llm_provider: LLM adapter 类型。
        logging: 日志输出策略。
        trace: Trace 导出策略。
        public_stream: 公开事件流及连接预算。
        projector_timeout_seconds: 单投影器调用期限。
        shutdown_timeout_seconds: 宿主关闭等待期限。
        tool_timeout_seconds: 默认工具执行期限。
        context_provider_timeout_seconds: 单 ContextProvider 调用期限。
        context_provider_max_content_chars: Provider 内容字符预算。
        middleware_llm_retry_limit: 中间件触发 LLM 重试的次数上限。

    以上 timeout 字段的时间单位均为秒。
    """

    model_config = ConfigDict(
        hide_input_in_errors=True,
    )

    redis: RuntimeRedisConfig
    database: MysqlDatabaseConfig | None = None
    asset: AssetLimitsConfig
    asset_store: RuntimeAssetStoreConfig
    managers: ManagerAdaptersConfig = Field(default_factory=ManagerAdaptersConfig)
    dispatcher: DispatcherConfig = Field(default_factory=DispatcherConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    memory: RuntimeMemoryConfig = Field(default_factory=RuntimeMemoryConfig)
    todo: RuntimeTodoConfig = Field(default_factory=RuntimeTodoConfig)
    llm_provider: RuntimeLLMProviderConfig = Field(
        default_factory=RuntimeLLMProviderConfig
    )
    logging: RuntimeLoggingConfig = Field(default_factory=RuntimeLoggingConfig)
    trace: RuntimeTraceConfig = Field(default_factory=RuntimeTraceConfig)
    public_stream: PublicStreamConfig = Field(default_factory=PublicStreamConfig)
    projector_timeout_seconds: float = Field(default=5.0, gt=0)
    shutdown_timeout_seconds: float = Field(default=10.0, gt=0)
    tool_timeout_seconds: float = Field(default=30.0, gt=0)
    context_provider_timeout_seconds: float = Field(default=5.0, gt=0)
    context_provider_max_content_chars: int = Field(default=20_000, gt=0)
    middleware_llm_retry_limit: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def validate_database_selection(self) -> Self:
        """校验 database 与实际使用数据库的组件选择一致。

        检查 Message/State、已启用的 Scheduler/Memory 以及 Todo 的存储选择。

        Returns:
            当前配置实例。

        Raises:
            ValueError: 使用数据库却缺少配置，或未使用数据库却提供了配置。
        """
        uses_database = (
            any(
                isinstance(manager, SqlManagerAdapterConfig)
                for manager in (self.managers.message, self.managers.state)
            )
            or (
                self.scheduler.enabled
                and isinstance(self.scheduler.store, SqlScheduleManagerConfig)
            )
            or (self.memory.enabled and self.memory.store.type == "db")
            or self.todo.store.type == "db"
        )
        if uses_database and self.database is None:
            raise ValueError("SQL Manager 必须配置 database")
        if not uses_database and self.database is not None:
            raise ValueError("未选择 SQL Manager 时不能配置 database")
        return self


class ModelParamsConfig(LLMGenerationConfig):
    """单 Agent 的模型参数，继承生成配置并补充连接和上下文预算。

    Attributes:
        type: 固定为 litellm 的 adapter 标记。
        api_key: 可选模型凭证，以 SecretStr 保存。
        base_url: 可选供应商基础地址。
        timeout_seconds: 模型调用等待秒数。
        context_window: 模型总上下文 token 预算，须为输入保留正数空间。
    """

    type: Literal["litellm"] = "litellm"
    api_key: SecretStr | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)
    context_window: int = Field(default=128_000, gt=0)

    @model_validator(mode="after")
    def validate_context_budget(self) -> Self:
        """验证模型总上下文减去最大输出后仍有正数输入预算。

        Returns:
            当前模型配置实例。

        Raises:
            ValueError: 已配置的 max_output_tokens 不小于 context_window。
        """
        if (
            self.max_output_tokens is not None
            and self.context_window - self.max_output_tokens <= 0
        ):
            raise ValueError("模型上下文必须为输入保留正数空间")
        return self

    def to_generation_config(self) -> LLMGenerationConfig:
        """仅复制 LLMGenerationConfig 声明的字段到独立生成配置。

        Returns:
            不包含连接凭证和本地上下文预算的新生成配置实例。
        """
        values = {
            field_name: getattr(self, field_name)
            for field_name in LLMGenerationConfig.model_fields
        }
        return LLMGenerationConfig.model_validate(values)


class AgentOutboundConfig(StrictModel):
    """选择代码中注册的公开事件投影器，不允许 YAML 指定任意 Python 导入。

    Attributes:
        type: 已注册 Factory 的稳定名称。
        options: 由该具体投影器进一步验证的 JSON 参数。
    """

    type: NonEmptyName
    options: dict[str, JsonValue] = Field(default_factory=dict)


class MediaArchiveRuleConfig(StrictModel):
    """按内容块种类和来源种类联合匹配的媒体归档规则。

    Attributes:
        block_types: 要匹配的媒体块类型，非空且无重复。
        source_types: 要匹配的 URL/base64 来源类型，非空且无重复。
    """

    block_types: tuple[Literal["image", "audio", "video", "document"], ...] = Field(
        min_length=1
    )
    source_types: tuple[Literal["url", "base64"], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_values(self) -> Self:
        """校验媒体归档的内容块和来源条件分别不重复。

        Returns:
            当前规则实例。

        Raises:
            ValueError: 任一条件元组中存在重复项。
        """
        if len(self.block_types) != len(set(self.block_types)):
            raise ValueError("block_types 包含重复项")
        if len(self.source_types) != len(set(self.source_types)):
            raise ValueError("source_types 包含重复项")
        return self


class AgentMediaConfig(StrictModel):
    """Agent 对输入媒体归档及 LLM 传输的策略，不执行媒体 I/O。

    Attributes:
        archive_rules: 需要归档的媒体匹配规则。
        llm_transport: 传递给模型 adapter 的媒体传输选择。
        include_asset_id_in_context: 是否在模型上下文中附带媒体资产标识。
    """

    archive_rules: tuple[MediaArchiveRuleConfig, ...] = ()
    llm_transport: MediaTransport = MediaTransport.AUTO
    include_asset_id_in_context: StrictBool = False


class CompactionConfig(StrictModel):
    """上下文压缩触发预算与确定性工具剪枝配置。

    Attributes:
        enabled: 是否启用上下文压缩。
        safety_margin_tokens: 为模型输入预留的安全 token 余量。
        trigger_ratio: 相对于输入预算的压缩触发比例。
        recent_message_ratio: 保留近端消息的预算比例，须小于触发比例。
        media_block_tokens: 单媒体块的估算 token 数。
        tool_argument_max_chars: 工具参数中每个字符串保留的前缀字符数；
            剪枝标记额外追加，不限制整个参数对象大小。
        tool_result_max_chars: 工具结果原文保留的前缀字符数，剪枝标记额外追加。
    """

    enabled: bool = True
    safety_margin_tokens: int = Field(default=2_048, gt=0)
    trigger_ratio: float = Field(default=0.80, gt=0, lt=1)
    recent_message_ratio: float = Field(default=0.20, gt=0, lt=1)
    media_block_tokens: int = Field(default=2_048, gt=0)
    tool_argument_max_chars: int = Field(default=512, gt=0)
    tool_result_max_chars: int = Field(default=2_048, gt=0)

    @model_validator(mode="after")
    def validate_ratios(self) -> Self:
        """验证近端消息保留比例严格小于压缩触发比例。

        Returns:
            当前压缩配置实例。

        Raises:
            ValueError: recent_message_ratio 不小于 trigger_ratio。
        """
        if self.recent_message_ratio >= self.trigger_ratio:
            raise ValueError("recent_message_ratio 必须严格小于 trigger_ratio")
        return self


class AgentConfig(StrictModel):
    """供 Agent 初始化和能力装配使用的配置，拒绝未知字段。

    本模型校验结构和组合约束；文件发现、插件注册和网络资源初始化由
    装配层负责，配置校验成功不等于外部依赖可用。

    Attributes:
        agent_id: Agent 稳定标识。
        agent_name: Agent 展示名称。
        prompt: Application 目录内 SYSTEM prompt 文件的相对路径。
        enabled_skills: 启用的 Skill 名称，顺序保留且无重复。
        skill_roots: 额外 Skill 搜索根目录。
        output_mode: 新会话使用的公开输出交付方式。
        outbound: 具体公开投影器选择及参数。
        enabled_tools: 工具名称星号模式；None 表示不筛选，空元组表示禁用。
        enabled_middlewares: 中间件名称星号模式，None 和空元组语义同工具。
        enabled_context_providers: 按配置顺序构建的 Provider 类型。
        plugin_options: Plugin 自定义键下的 JSON 配置。
        llm_model: 模型连接、生成参数及上下文预算。
        compaction: 压缩和工具内容剪枝策略。
        media: 媒体归档及传输策略。
        memory: 当前 Agent 的记忆作用域及额度。
        metadata: 应用自定义 JSON 元信息。
    """

    agent_id: NonEmptyName
    agent_name: NonEmptyName
    prompt: NonEmptyName
    enabled_skills: tuple[NonEmptyName, ...] = ()
    skill_roots: tuple[NonEmptyName, ...] = ()
    output_mode: OutputMode = OutputMode.COMPLETE
    outbound: AgentOutboundConfig
    enabled_tools: tuple[NonEmptyName, ...] | None = None
    enabled_middlewares: tuple[NonEmptyName, ...] | None = None
    enabled_context_providers: tuple[NonEmptyName, ...] = ()
    knowledge_bases: tuple[KnowledgeBaseRef, ...] = ()
    plugin_options: dict[NonEmptyName, dict[str, JsonValue]] = Field(
        default_factory=dict
    )
    llm_model: ModelParamsConfig
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    media: AgentMediaConfig = Field(default_factory=AgentMediaConfig)
    memory: AgentMemoryConfig = Field(default_factory=AgentMemoryConfig)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("enabled_tools", "enabled_middlewares")
    @classmethod
    def validate_capability_patterns(
        cls,
        values: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        """校验工具和中间件筛选模式只使用星号通配语义。

        Args:
            values: 模式元组；None 表示未限制能力集合。

        Returns:
            原始模式元组或 None，不在此验证模式能否匹配能力。

        Raises:
            ValueError: 存在问号、方括号或反斜杠语法。
        """
        if values is None:
            return None
        unsupported = frozenset("?[]\\")
        for value in values:
            if any(character in value for character in unsupported):
                raise ValueError("Capability pattern 只支持 * 通配符")
        return values

    @model_validator(mode="after")
    def validate_unique_capabilities(self) -> Self:
        """校验 Skill 根目录和各能力列表无重复项，不改变原始顺序。

        Returns:
            当前 Agent 配置实例。

        Raises:
            ValueError: 任一受检查的能力列表包含重复值。
        """
        collections: tuple[tuple[str, tuple[str, ...] | None], ...] = (
            ("enabled_skills", self.enabled_skills),
            ("skill_roots", self.skill_roots),
            ("enabled_tools", self.enabled_tools),
            ("enabled_middlewares", self.enabled_middlewares),
            ("enabled_context_providers", self.enabled_context_providers),
        )
        for field_name, values in collections:
            if values is None:
                continue
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} 包含重复项")
        knowledge_names = tuple(ref.name for ref in self.knowledge_bases)
        if len(knowledge_names) != len(set(knowledge_names)):
            raise ValueError("knowledge_bases 包含重复项")
        return self
