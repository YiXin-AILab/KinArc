"""与 Redis 等具体队列实现无关的 Inbox 领域模型。"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from agentsystem.common.models import FrozenModel, JsonValue, OutputMode, SessionContext
from agentsystem.message.models import UserContentBlock


class InboundMode(StrEnum):
    """普通消息的领取时机。

    STEER 可在必须继续的 Tool loop 中领取，QUEUE 留待 run 边界由 Session
    领取；两者都可作为后续 run 的初始输入。
    """

    STEER = "steer"
    QUEUE = "queue"


class InboundRole(StrEnum):
    """公开 Inbox 可注入的 USER 和 SYSTEM 角色。

    动态 SYSTEM 输入仍遵循普通输入的排队和持久化流程。
    """

    USER = "user"
    SYSTEM = "system"


class InboundMessage(FrozenModel):
    """Application 写入普通 Inbox 的不可变输入信封，不包含内部 run_id。

    Attributes:
        message_id: 用于幂等入队和完成确认的消息标识。
        session_id: 目标会话，写入时须与 SessionContext 匹配。
        role: USER 或动态 SYSTEM 输入角色。
        content: 非空的输入内容块序列。
        origin: 输入来源标记。
        mode: 运行中插入或 run 边界排队策略。
        created_at: 可选来源时间，模型不自动填充。
        metadata: 额外 JSON 元信息。
    """

    message_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    role: InboundRole = InboundRole.USER
    content: tuple[UserContentBlock, ...] = Field(min_length=1)
    origin: str
    mode: InboundMode = InboundMode.QUEUE
    created_at: datetime | None = None
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)


class ReadyKind(StrEnum):
    """Ready 控制消息的联合类型判别值。

    NORMAL 唤醒普通输入处理，RESUME 请求恢复已有挂起 run。
    """

    NORMAL = "normal"
    RESUME = "resume"


class NormalReady(FrozenModel):
    """只唤醒目标会话的普通 ready，不携带业务消息或 run_id。

    Attributes:
        kind: 固定为 NORMAL 的判别值。
        app_id: 应用标识。
        user_id: 用户标识。
        agent_id: Agent 标识，同时用于选择 Ready Stream。
        session_id: 待唤醒的会话标识。
    """

    kind: Literal[ReadyKind.NORMAL] = ReadyKind.NORMAL
    app_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class ResumeReady(FrozenModel):
    """通过 Ready 通道恢复 PENDING run 的控制消息，不进入普通 Inbox。

    Attributes:
        kind: 固定为 RESUME 的判别值。
        resume_id: 本次恢复请求标识，锁忙重投时保持不变。
        app_id: 应用标识。
        user_id: 用户标识。
        agent_id: Agent 标识。
        session_id: 目标会话标识。
        run_id: 待恢复的已有 run 标识。
        resume_token: 待校验的单次恢复凭证。
        input: 恢复处理使用的 JSON 输入。
    """

    kind: Literal[ReadyKind.RESUME] = ReadyKind.RESUME
    resume_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    resume_token: str = Field(min_length=1)
    input: Mapping[str, JsonValue] = Field(default_factory=dict)


type ReadyEntry = Annotated[
    NormalReady | ResumeReady,
    Field(discriminator="kind"),
]


class RunRequest(FrozenModel):
    """AgentSession 持锁并领取输入后构造的内部 run 描述。

    Attributes:
        ctx: 完整会话作用域。
        run_id: 本次 run 的内部标识。
        trace_id: Runtime 执行段标识，与 OpenTelemetry TraceId 不同。
        output_mode: 会话确定的输出交付模式。
        inbox_owner_token: 可选的 Inbox 领取令牌，供消息保存后确认完成。
    """

    ctx: SessionContext
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    output_mode: OutputMode
    inbox_owner_token: str | None = Field(default=None, min_length=1)


class ResumeRequest(FrozenModel):
    """请求边界校验 PENDING state 后交给队列的内部恢复请求。

    Attributes:
        ctx: 完整会话作用域。
        resume_id: 恢复请求标识。
        run_id: 要继续执行的已有 run 标识。
        resume_token: 在实际恢复边界再次验证的单次凭证。
        input: 恢复处理使用的 JSON 输入。
    """

    ctx: SessionContext
    resume_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    resume_token: str = Field(min_length=1)
    input: Mapping[str, JsonValue] = Field(default_factory=dict)
