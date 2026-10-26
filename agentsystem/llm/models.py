"""LLM 生成参数、使用量、请求、响应与事件的统一模型。"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from agentsystem.common.errors import LLMError
from agentsystem.common.models import FinishReason, JsonValue, OutputMode, StrictModel
from agentsystem.message.models import AssistantBlock, BlockType, Message


class UsageAccounting(StrEnum):
    """提供方 token 统计的完整程度。

    EXACT 要求输入输出之和等于总数；PARTIAL 表示部分统计，TOTAL_ONLY 表示仅总数。
    """

    EXACT = "exact"
    PARTIAL = "partial"
    TOTAL_ONLY = "total_only"


class MediaTransport(StrEnum):
    """向模型提供多模态内容时使用的传输策略。

    AUTO 可透传非音频 URL；BASE64 要求解析正文。资产和音频由 Client 转为 Base64。
    """

    AUTO = "auto"
    BASE64 = "base64"


class Usage(StrictModel):
    """一次逻辑 LLM 调用的 token 使用量，不承担计费或聚合。

    Attributes:
        input_tokens: 输入 token 数。
        output_tokens: 输出 token 数。
        total_tokens: 提供方报告的总量。
        accounting: 统计完整程度，只有 EXACT 强制校验加和。
        reasoning_tokens: 可选推理 token 数，缺失与零有不同语义。
        cache_read_tokens: 可选缓存读取 token 数。
        cache_write_tokens: 可选缓存写入 token 数。
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    accounting: UsageAccounting = UsageAccounting.PARTIAL
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_exact_total(self) -> Self:
        """确保精确统计的总数可由输入和输出完整解释。

        Returns:
            校验通过的当前记录。

        Raises:
            ValueError: EXACT 统计的总量与输入输出之和不符。
        """
        expected_total = self.input_tokens + self.output_tokens
        if (
            self.accounting is UsageAccounting.EXACT
            and self.total_tokens != expected_total
        ):
            raise ValueError("精确统计的总 token 数必须等于输入与输出 token 数之和")
        return self


class Timing(StrictModel):
    """一次外部调用的时间记录，不自行读取时钟。

    Attributes:
        started_at: 调用开始的墙上时钟时间。
        ended_at: 调用结束的墙上时钟时间。
        duration_ms: 实测非负耗时，单位毫秒。
        first_token_ms: 可选首个有效内容片段延迟，单位毫秒。
    """

    started_at: datetime
    ended_at: datetime
    duration_ms: float = Field(ge=0)
    first_token_ms: float | None = Field(default=None, ge=0)


class LLMGenerationConfig(StrictModel):
    """与具体 Provider SDK 解耦的单次生成参数。

    Attributes:
        model: 提供方模型标识。
        temperature: 可选采样温度，None 留给提供方决定。
        max_output_tokens: 输出 token 上限。
        stop: 停止字符串集合。
        tool_choice: 提供方支持的 JSON 工具选择策略。
        parallel_tool_calls: 是否请求并行工具调用，None 表示不覆盖默认行为。
        provider_options: adapter 边界解释的附加参数，不能覆盖其保留参数。
    """

    model: str = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=16_384, gt=0)
    stop: tuple[str, ...] = ()
    tool_choice: JsonValue = None
    parallel_tool_calls: bool | None = None
    provider_options: Mapping[str, JsonValue] = Field(default_factory=dict)


class LLMRequest(StrictModel):
    """AgentLoop 发送给 LLMProvider 的完整请求。

    Attributes:
        request_id: 单次逻辑调用的关联标识。
        messages: 有序模型上下文快照，至少包含一条消息。
        tools: 已渲染的工具声明。
        config: 本次生成参数。
        output_mode: Runtime 选择的输出模式。
    """

    request_id: str = Field(min_length=1)
    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[Mapping[str, JsonValue], ...] = ()
    config: LLMGenerationConfig
    output_mode: OutputMode


class LLMResponse(StrictModel):
    """Provider 归一化后唯一可信的完整响应，供持久化和控制流消费。

    Attributes:
        blocks: 完整 Assistant 内容块，错误时可以为空。
        usage: 本次调用的 token 统计。
        timing: 调用时刻与耗时。
        model: 实际或请求使用的模型名。
        finish_reason: 统一结束原因。
        response_id: 可选上游响应标识。
        error: 仅 ERROR 结束原因对应的结构化错误。
        raw: adapter 调试数据，排除在序列化和 repr 之外。
    """

    blocks: tuple[AssistantBlock, ...]
    usage: Usage = Field(default_factory=Usage)
    timing: Timing
    model: str = Field(min_length=1)
    finish_reason: FinishReason
    response_id: str | None = None
    error: LLMError | None = None
    raw: Any = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_error_contract(self) -> Self:
        """ERROR finish reason 与结构化错误必须同时出现。

        Returns:
            校验通过的响应本身。

        Raises:
            ValueError: ERROR 结束原因与 error 字段不匹配。
        """
        if (self.finish_reason is FinishReason.ERROR) != (self.error is not None):
            raise ValueError("finish_reason=ERROR 与 error 必须同时出现")
        return self


class LLMEventBase(StrictModel):
    """单次 Provider 调用内所有事件共享的关联字段。

    Attributes:
        request_id: 与输入请求一致的逻辑调用标识。
        created_at: 事件生成时间。
    """

    request_id: str = Field(min_length=1)
    created_at: datetime


class LLMStart(LLMEventBase):
    """Provider 开始处理一次逻辑请求的事件。

    Attributes:
        type: 固定事件判别值。
        model: 请求模型名。
        output_mode: 本次请求的 Runtime 输出模式。
    """

    type: Literal["llm_start"] = "llm_start"
    model: str = Field(min_length=1)
    output_mode: OutputMode


class LLMBlockStart(LLMEventBase):
    """一个 Assistant Block 的边界开始事件。

    Attributes:
        type: 固定事件判别值。
        index: 单次响应内从零开始的块序号。
        block_id: 关联后续增量与结束事件的块标识。
        block_type: 规范化块类型。
        tool_name: 工具调用块已知的工具名称，否则为空。
        tool_call_id: 工具调用块已知的调用标识，否则为空。
    """

    type: Literal["block_start"] = "block_start"
    index: int = Field(ge=0)
    block_id: str = Field(min_length=1)
    block_type: BlockType
    tool_name: str | None = Field(default=None, min_length=1)
    tool_call_id: str | None = Field(default=None, min_length=1)


class LLMBlockDelta(LLMEventBase):
    """属于一个 Block 的增量文本，不能代替完整响应。

    Attributes:
        type: 固定事件判别值。
        index: 响应内的块序号。
        block_id: 对应开始事件的块标识。
        block_type: 文本、思考或工具调用类型。
        delta: 新增内容；工具块中为尚未解析的参数片段。
    """

    type: Literal["block_delta"] = "block_delta"
    index: int = Field(ge=0)
    block_id: str = Field(min_length=1)
    block_type: BlockType
    delta: str


class LLMBlockEnd(LLMEventBase):
    """一个 Block 的完整归一化结果。

    Attributes:
        type: 固定事件判别值。
        index: 响应内的块序号。
        block_id: 对应开始事件的块标识。
        block: 聚合并解析完成的 Assistant 内容块。
    """

    type: Literal["block_end"] = "block_end"
    index: int = Field(ge=0)
    block_id: str = Field(min_length=1)
    block: AssistantBlock


class LLMEnd(LLMEventBase):
    """两种输出模式唯一可信的逻辑调用终态。

    Attributes:
        type: 固定事件判别值。
        response: 包含完整块、用量、时间和结束原因的权威响应。
    """

    type: Literal["llm_end"] = "llm_end"
    response: LLMResponse


type LLMEvent = Annotated[
    LLMStart | LLMBlockStart | LLMBlockDelta | LLMBlockEnd | LLMEnd,
    Field(discriminator="type"),
]
