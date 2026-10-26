"""AgentState 模型、纯生命周期转换与终态派生计时。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hmac import compare_digest
from typing import Self

from pydantic import Field, model_validator

from agentsystem.common.errors import RunError
from agentsystem.common.models import FrozenModel, OutputMode, SessionContext
from agentsystem.llm.models import Usage


class SessionStatus(StrEnum):
    """标识 Session 最新 run 的生命周期，不表示整个会话被永久关闭。

    IDLE 尚无 run；RUNNING 正在执行；PENDING 等待工具恢复；SUCCEEDED、
    FAILED、CANCELED 为 run 终态，后续普通输入可启动新的 run。
    """

    IDLE = "idle"
    RUNNING = "running"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class AgentState(FrozenModel):
    """保存一个 session 最新 run 的不可变执行控制快照。

    消息正文由 MessageManager 管理；本模型仅保存引用与生命周期字段。
    状态转换返回新实例，持久化及并发控制由 StateManager 和 Session 负责。

    Attributes:
        session_context: 稳定的应用、用户、Agent 和会话定位。
        status: 最新 run 的生命周期状态。
        run_id: 内部 run 标识；IDLE 时为空。
        output_mode: 会话沿用的输出模式。
        turn_index: 当前 run 已开始的轮次，尚未开始时为零。
        current_message_ids: 当前轮需要突出展示的输入引用。
        steer_message_ids: 下一轮待提升为 current 的 STEER 输入引用。
        usage_total: 当前 run 累计模型用量，包含压缩调用。
        context_tokens: 最近一次可复用的上下文 token 数。
        context_token_checkpoint_message_id: token 统计覆盖到的消息。
        context_token_request_fingerprint: 校验检查点适用性的请求指纹。
        final_message_id: 成功终态的最终 Assistant 消息引用。
        started_at: run 首次启动时间，恢复时保留。
        ended_at: 终态时间，活动状态为空。
        active_started_at: 当前执行段起点，仅 RUNNING 时存在。
        active_duration_ms: 已完成执行段的累计毫秒数，不含挂起等待。
        error: 失败或取消的结构化原因。
        pending_tool_call_id: PENDING 时唯一待恢复的工具调用。
        resume_token: PENDING 时保存的单次恢复凭证。
        suspend_expires_at: 可选恢复期限，到期后拒绝恢复。
    """

    session_context: SessionContext
    status: SessionStatus = SessionStatus.IDLE
    run_id: str | None = Field(default=None, min_length=1)
    output_mode: OutputMode | None = None
    turn_index: int = Field(default=0, ge=0)
    current_message_ids: tuple[str, ...] = ()
    steer_message_ids: tuple[str, ...] = ()
    usage_total: Usage = Field(default_factory=Usage)
    context_tokens: int | None = Field(default=None, ge=0)
    context_token_checkpoint_message_id: str | None = Field(
        default=None,
        min_length=1,
    )
    context_token_request_fingerprint: str | None = Field(
        default=None,
        min_length=1,
    )
    final_message_id: str | None = Field(default=None, min_length=1)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    active_started_at: datetime | None = None
    active_duration_ms: float = Field(default=0, ge=0)
    error: RunError | None = None
    pending_tool_call_id: str | None = Field(default=None, min_length=1)
    resume_token: str | None = Field(default=None, min_length=1)
    suspend_expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_status_contract(self) -> Self:
        """校验完整快照的生命周期字段组合。

        Returns:
            校验通过的当前实例。

        Raises:
            ValueError: 标识、计时、检查点或挂起字段与状态不一致。
        """
        if self.status is SessionStatus.IDLE:
            self._validate_idle()
        else:
            self._validate_run_identity()
        self._validate_context_token_checkpoint()
        self._validate_pending_fields()
        self._validate_terminal_fields()
        return self

    def _validate_context_token_checkpoint(self) -> None:
        checkpoint = (
            self.context_tokens,
            self.context_token_checkpoint_message_id,
            self.context_token_request_fingerprint,
        )
        if any(value is None for value in checkpoint) and any(
            value is not None for value in checkpoint
        ):
            raise ValueError("context token checkpoint 字段必须同时存在或为空")

    def _validate_idle(self) -> None:
        run_fields = (
            self.run_id,
            self.started_at,
            self.ended_at,
            self.active_started_at,
        )
        if any(value is not None for value in run_fields):
            raise ValueError("IDLE 状态不能包含 run 数据")
        if self.active_duration_ms != 0:
            raise ValueError("IDLE 状态不能包含 active 耗时")

    def _validate_run_identity(self) -> None:
        if self.run_id is None or self.output_mode is None or self.started_at is None:
            raise ValueError("非 IDLE 状态必须包含 run_id、output_mode 和 started_at")

    def _validate_pending_fields(self) -> None:
        pending_fields = (
            self.pending_tool_call_id,
            self.resume_token,
            self.suspend_expires_at,
        )
        if self.status is SessionStatus.PENDING:
            if self.pending_tool_call_id is None or self.resume_token is None:
                raise ValueError("PENDING 状态缺少挂起工具或恢复令牌")
            return
        if any(value is not None for value in pending_fields):
            raise ValueError("非 PENDING 状态不能包含挂起字段")

    def _validate_terminal_fields(self) -> None:
        terminal_statuses = {
            SessionStatus.SUCCEEDED,
            SessionStatus.FAILED,
            SessionStatus.CANCELED,
        }
        if self.status in terminal_statuses and self.ended_at is None:
            raise ValueError("终态必须包含 ended_at")
        if (
            self.status in {SessionStatus.RUNNING, SessionStatus.PENDING}
            and self.ended_at is not None
        ):
            raise ValueError("活动状态不能包含 ended_at")
        if self.status is SessionStatus.RUNNING and self.active_started_at is None:
            raise ValueError("RUNNING 状态必须包含 active_started_at")
        if (
            self.status is not SessionStatus.RUNNING
            and self.active_started_at is not None
        ):
            raise ValueError("非 RUNNING 状态不能包含 active_started_at")
        if self.status is SessionStatus.SUCCEEDED and self.final_message_id is None:
            raise ValueError("SUCCEEDED 状态必须包含 final_message_id")
        if self.status is SessionStatus.FAILED and self.error is None:
            raise ValueError("FAILED 状态必须包含 error")


@dataclass(frozen=True, slots=True, kw_only=True)
class RunTiming:
    """保存终态 run 的不可变计时结果，不负责读取时钟或持久化。

    Attributes:
        started_at: run 首次开始时间。
        ended_at: run 进入终态的时间。
        elapsed_ms: 包含挂起等待的墙钟毫秒数。
        active_ms: 各执行段累计毫秒数，不得超过 elapsed_ms。
    """

    started_at: datetime
    ended_at: datetime
    elapsed_ms: float
    active_ms: float

    def __post_init__(self) -> None:
        """校验累计执行时间不超过包含等待的墙钟时间。

        Raises:
            ValueError: active_ms 大于 elapsed_ms。
        """
        if self.active_ms > self.elapsed_ms:
            raise ValueError("active_ms 不能超过 elapsed_ms")


def run_timing(state: AgentState) -> RunTiming:
    """从已保存终态 State 构造公开 RunTiming。

    Args:
        state: 已进入成功、失败或取消终态的最新状态。

    Returns:
        包含墙钟与 active 双口径的不可变统计。

    Raises:
        ValueError: State 尚未形成完整终态时间，或时钟发生倒退。
    """
    if state.started_at is None or state.ended_at is None:
        raise ValueError("只有终态 State 可以生成 RunTiming")
    elapsed_ms = (state.ended_at - state.started_at).total_seconds() * 1_000
    if elapsed_ms < 0:
        raise ValueError("run 计时时钟发生倒退")
    return RunTiming(
        started_at=state.started_at,
        ended_at=state.ended_at,
        elapsed_ms=elapsed_ms,
        active_ms=state.active_duration_ms,
    )


_RESTARTABLE_STATUSES = {
    SessionStatus.IDLE,
    SessionStatus.SUCCEEDED,
    SessionStatus.FAILED,
    SessionStatus.CANCELED,
}


def start_run(
    state: AgentState,
    *,
    run_id: str,
    output_mode: OutputMode,
    now: datetime,
) -> AgentState:
    """从空闲或终态生成新 run 快照，保留已有输出模式和上下文检查点。

    Args:
        state: 尚未启动或已结束上一 run 的会话快照。
        run_id: 已持锁领取非空输入后生成的新 run 标识。
        output_mode: 尚未固定输出模式时使用的模式。
        now: 新 run 和首个执行段的起点。

    Returns:
        重置 run 范围字段后的 RUNNING 快照；输入实例不变。

    Raises:
        ValueError: 原状态仍活动、run_id 为空或新快照无效。
    """
    if state.status not in _RESTARTABLE_STATUSES:
        raise ValueError("只有 IDLE 或终态可以开始新 run")
    if not run_id:
        raise ValueError("run_id 不能为空")
    resolved_output_mode = state.output_mode or output_mode
    return _replace_state(
        state,
        status=SessionStatus.RUNNING,
        run_id=run_id,
        output_mode=resolved_output_mode,
        turn_index=0,
        current_message_ids=(),
        steer_message_ids=(),
        usage_total=Usage(),
        final_message_id=None,
        started_at=now,
        ended_at=None,
        active_started_at=now,
        active_duration_ms=0,
        error=None,
        pending_tool_call_id=None,
        resume_token=None,
        suspend_expires_at=None,
    )


def start_turn(state: AgentState) -> AgentState:
    """进入下一 turn，并在存在 STEER 时将其提升为 current。

    Args:
        state: 当前 RUNNING 快照。

    Returns:
        轮次加一且清空 STEER 引用的新快照；无 STEER 时保留 current。

    Raises:
        ValueError: 当前状态不是 RUNNING。
    """
    _require_running(state)
    current_message_ids = state.current_message_ids
    if state.steer_message_ids:
        current_message_ids = state.steer_message_ids
    return _replace_state(
        state,
        turn_index=state.turn_index + 1,
        current_message_ids=current_message_ids,
        steer_message_ids=(),
    )


def set_context_token_checkpoint(
    state: AgentState,
    *,
    tokens: int,
    message_id: str,
    request_fingerprint: str,
) -> AgentState:
    """设置已经覆盖到指定 Message 的上下文 token 检查点。

    Args:
        state: 需要更新统计的快照。
        tokens: 已覆盖上下文的非负 token 数。
        message_id: 统计覆盖边界的消息标识。
        request_fingerprint: 与统计匹配的模型请求指纹。

    Returns:
        三个检查点字段同时更新的新快照。

    Raises:
        ValueError: token 数为负或任一身份字段为空。
    """
    if tokens < 0:
        raise ValueError("context token checkpoint 不能为负数")
    if not message_id or not request_fingerprint:
        raise ValueError("context token checkpoint 身份不能为空")
    return _replace_state(
        state,
        context_tokens=tokens,
        context_token_checkpoint_message_id=message_id,
        context_token_request_fingerprint=request_fingerprint,
    )


def clear_context_token_checkpoint(state: AgentState) -> AgentState:
    """清除不能继续复用的上下文 token 检查点。

    Args:
        state: 含有过期或不可复用统计的快照。

    Returns:
        三个检查点字段同时置空的新快照。
    """
    return _replace_state(
        state,
        context_tokens=None,
        context_token_checkpoint_message_id=None,
        context_token_request_fingerprint=None,
    )


def suspend_run(
    state: AgentState,
    *,
    tool_call_id: str,
    resume_token: str,
    now: datetime,
    expires_at: datetime | None = None,
) -> AgentState:
    """结束当前执行段并生成带单次恢复凭证的 PENDING 快照。

    Args:
        state: 当前 RUNNING 快照。
        tool_call_id: 唯一挂起工具调用的标识。
        resume_token: 后续恢复必须匹配的非空凭证。
        now: 本执行段结束时间。
        expires_at: 可选恢复期限。

    Returns:
        累加执行时间并保存挂起字段的新快照。

    Raises:
        ValueError: 状态非 RUNNING、身份字段为空或执行段时钟倒退。
    """
    _require_running(state)
    if not tool_call_id:
        raise ValueError("tool_call_id 不能为空")
    if not resume_token:
        raise ValueError("resume_token 不能为空")
    return _replace_state(
        state,
        status=SessionStatus.PENDING,
        active_duration_ms=_finish_active_segment(state, now),
        active_started_at=None,
        pending_tool_call_id=tool_call_id,
        resume_token=resume_token,
        suspend_expires_at=expires_at,
    )


def resume_run(
    state: AgentState,
    *,
    resume_token: str,
    now: datetime,
) -> AgentState:
    """校验凭证并生成同一 run 的恢复快照，不执行持久化 CAS。

    Args:
        state: 保存恢复凭证的 PENDING 快照。
        resume_token: 请求提交的单次凭证。
        now: 恢复执行段起点，也用于判断凭证是否到期。

    Returns:
        清除全部挂起字段后的 RUNNING 快照。

    Raises:
        ValueError: 当前非 PENDING、凭证不匹配或已到期。
    """
    _require_pending(state)
    expected_token = state.resume_token
    if expected_token is None or not compare_digest(expected_token, resume_token):
        raise ValueError("resume token 不匹配")
    if state.suspend_expires_at is not None and now >= state.suspend_expires_at:
        raise ValueError("resume token 已过期")
    return _replace_state(
        state,
        status=SessionStatus.RUNNING,
        active_started_at=now,
        pending_tool_call_id=None,
        resume_token=None,
        suspend_expires_at=None,
    )


def complete_run(
    state: AgentState,
    *,
    final_message_id: str,
    now: datetime,
) -> AgentState:
    """将 RUNNING run 定稿为成功，并清除输入引用与挂起字段。

    Args:
        state: 当前 RUNNING 快照。
        final_message_id: 已完成 Assistant 消息的非空标识。
        now: run 和当前执行段结束时间。

    Returns:
        包含最终消息引用和累计执行时间的 SUCCEEDED 快照。

    Raises:
        ValueError: 状态不合法、消息标识为空或执行段时钟倒退。
    """
    if not final_message_id:
        raise ValueError("final_message_id 不能为空")
    return _finish_run(
        state,
        status=SessionStatus.SUCCEEDED,
        now=now,
        final_message_id=final_message_id,
    )


def fail_run(
    state: AgentState,
    *,
    error: RunError,
    now: datetime,
) -> AgentState:
    """将 RUNNING 或 PENDING run 定稿为失败。

    Args:
        state: 当前活动快照。
        error: 保存到终态的结构化失败原因。
        now: run 结束时间；RUNNING 时也结束当前执行段。

    Returns:
        清空输入引用和挂起字段的 FAILED 快照。

    Raises:
        ValueError: 状态非活动态或执行段时钟倒退。
    """
    return _finish_run(
        state,
        status=SessionStatus.FAILED,
        now=now,
        error=error,
    )


def cancel_run(
    state: AgentState,
    *,
    now: datetime,
    error: RunError | None = None,
) -> AgentState:
    """将 RUNNING 或 PENDING run 定稿为取消。

    Args:
        state: 当前活动快照。
        now: run 结束时间；RUNNING 时也结束当前执行段。
        error: 可选的结构化取消原因。

    Returns:
        清空输入引用和挂起字段的 CANCELED 快照。

    Raises:
        ValueError: 状态非活动态或执行段时钟倒退。
    """
    return _finish_run(
        state,
        status=SessionStatus.CANCELED,
        now=now,
        error=error,
    )


def converge_orphaned_run(state: AgentState, *, now: datetime) -> AgentState:
    """把崩溃后遗留的 RUNNING run 转换为 orphaned_run 失败快照。

    Args:
        state: 已由调用方确认失去执行者的 RUNNING 快照。
        now: 收敛时刻，用于结束执行段及 run 计时。

    Returns:
        带 orphaned_run 原因的 FAILED 快照，不直接持久化。

    Raises:
        ValueError: 状态不是 RUNNING 或执行段时钟倒退。
    """
    _require_running(state)
    error = RunError(
        code="orphaned_run",
        message="检测到未正常结束的遗留 run",
    )
    return fail_run(state, error=error, now=now)


def _replace_state(state: AgentState, **changes: object) -> AgentState:
    """合并更新并重新校验完整 AgentState 不变量。"""
    values = state.model_dump(mode="python")
    values.update(changes)
    return AgentState.model_validate(values)


def _finish_run(
    state: AgentState,
    *,
    status: SessionStatus,
    now: datetime,
    final_message_id: str | None = None,
    error: RunError | None = None,
) -> AgentState:
    if status is SessionStatus.SUCCEEDED:
        _require_running(state)
    else:
        _require_active(state)
    active_duration_ms = state.active_duration_ms
    if state.status is SessionStatus.RUNNING:
        active_duration_ms = _finish_active_segment(state, now)
    return _replace_state(
        state,
        status=status,
        current_message_ids=(),
        steer_message_ids=(),
        final_message_id=final_message_id,
        ended_at=now,
        active_started_at=None,
        active_duration_ms=active_duration_ms,
        error=error,
        pending_tool_call_id=None,
        resume_token=None,
        suspend_expires_at=None,
    )


def _require_running(state: AgentState) -> None:
    if state.status is not SessionStatus.RUNNING:
        raise ValueError("状态转换要求当前状态为 RUNNING")


def _require_pending(state: AgentState) -> None:
    if state.status is not SessionStatus.PENDING:
        raise ValueError("状态转换要求当前状态为 PENDING")


def _require_active(state: AgentState) -> None:
    if state.status not in {SessionStatus.RUNNING, SessionStatus.PENDING}:
        raise ValueError("状态转换要求当前状态为 RUNNING 或 PENDING")


def _finish_active_segment(state: AgentState, now: datetime) -> float:
    started_at = state.active_started_at
    if started_at is None:
        raise ValueError("RUNNING 状态缺少 active_started_at")
    elapsed_ms = (now - started_at).total_seconds() * 1_000
    if elapsed_ms < 0:
        raise ValueError("active 计时时钟发生倒退")
    return state.active_duration_ms + elapsed_ms
