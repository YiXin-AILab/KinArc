"""定时任务与执行实例的领域模型。"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from agentsystem.common.models import FrozenModel, IdentityText, StrictModel

type NonEmptyText = Annotated[str, Field(min_length=1)]
type Identifier = Annotated[IdentityText, Field(max_length=255)]


class ScheduleTaskType(StrEnum):
    """定时任务类型。

    Attributes:
        RECURRING: 每次命中分钟均可物化执行实例。
        ONE_TIME: 首次自动物化后任务进入 completed。
    """

    RECURRING = "recurring"
    ONE_TIME = "one_time"


class ScheduleSessionMode(StrEnum):
    """定时任务会话模式。

    Attributes:
        ISOLATED: 每个执行实例使用独立会话。
        FIXED: 使用任务绑定的固定会话。
    """

    ISOLATED = "isolated"
    FIXED = "fixed"


class ScheduleTaskStatus(StrEnum):
    """定时任务状态。

    Attributes:
        ENABLED: 允许自动扫描触发。
        DISABLED: 停止自动触发，未删除时仍可手动运行。
        COMPLETED: 一次性任务已完成自动物化。
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    COMPLETED = "completed"


class ScheduleRunStatus(StrEnum):
    """任务执行实例状态。

    Attributes:
        PENDING: 已物化，尚未由 Runtime 开始追踪。
        RUNNING: 已绑定 Runtime run 并开始执行。
        SUCCEEDED: Runtime 成功终态。
        FAILED: Runtime 失败终态。
        CANCELED: Runtime 取消终态。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ScheduleTriggerKind(StrEnum):
    """执行实例的触发来源。

    Attributes:
        SCHEDULED: Cron 命中当前分钟。
        MANUAL: 对话工具显式立即运行。
    """

    SCHEDULED = "scheduled"
    MANUAL = "manual"


class DispatchResult(StrEnum):
    """MessageQueue 单次投递结果。

    Attributes:
        ACCEPTED: Inbox 接受本次投递。
        FAILED: 本次投递失败，Run 仍保留 pending。
        ALREADY_MATERIALIZED: 相同 occurrence 已存在，本次未重复投递。
    """

    ACCEPTED = "accepted"
    FAILED = "failed"
    ALREADY_MATERIALIZED = "already_materialized"


class ScheduleTask(FrozenModel):
    """已持久化的定时任务事实。

    Attributes:
        task_id: 任务唯一 ID。
        app_id: 应用命名空间。
        user_id: 任务所属用户，用于访问隔离和执行身份快照。
        agent_id: 所属 Agent，与应用、用户共同限定查询范围。
        name: 供用户识别的任务名称。
        prompt: 触发时提交给 Agent 的文本。
        cron_expression: 部署时区下的标准五段 Cron。
        task_type: 周期或一次性任务。
        session_mode: 独立或固定会话策略。
        fixed_session_id: 固定会话 ID；独立模式必须为 None。
        status: 任务启用、停用或一次性完成状态。
        version: 修改和删除时比较的正整数版本。
        created_at: 带时区的创建时间。
        updated_at: 最近修改时间，也是列表排序键。
        deleted_at: 软删除时间；非空时任务必须停用。
    """

    task_id: Identifier
    app_id: Identifier
    user_id: Identifier
    agent_id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=255)]
    prompt: NonEmptyText
    cron_expression: Annotated[str, Field(min_length=1, max_length=255)]
    task_type: ScheduleTaskType
    session_mode: ScheduleSessionMode
    fixed_session_id: Identifier | None = None
    status: ScheduleTaskStatus
    version: Annotated[int, Field(ge=1)]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    deleted_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_session_and_status(self) -> Self:
        """校验会话绑定与 one-time 终态。

        Returns:
            校验通过的当前模型实例。

        Raises:
            ValueError: 字段组合不满足模型约束。
        """
        if self.session_mode is ScheduleSessionMode.FIXED:
            if self.fixed_session_id is None:
                raise ValueError("fixed 模式必须提供 fixed_session_id")
        elif self.fixed_session_id is not None:
            raise ValueError("isolated 模式的 fixed_session_id 必须为空")
        if (
            self.status is ScheduleTaskStatus.COMPLETED
            and self.task_type is not ScheduleTaskType.ONE_TIME
        ):
            raise ValueError("completed 状态只属于 one_time 任务")
        if (
            self.deleted_at is not None
            and self.status is not ScheduleTaskStatus.DISABLED
        ):
            raise ValueError("软删除任务必须是 disabled")
        return self


class ScheduleRun(FrozenModel):
    """一次已物化的定时任务执行实例。

    Attributes:
        schedule_run_id: 执行实例唯一 ID，区别于 Runtime run ID。
        task_id: 来源任务 ID。
        occurrence_key: 同一任务内的触发去重键。
        trigger_kind: 自动或手动触发来源。
        scheduled_for: 本次计划执行时间。
        task_name_snapshot: 物化时的任务名称。
        app_id_snapshot: 物化时的应用身份。
        user_id_snapshot: 物化时的用户身份。
        agent_id_snapshot: 物化时的 Agent 身份。
        prompt_snapshot: 物化时的输入文本，后续任务修改不影响它。
        session_id: 本次输入投递的会话。
        message_id: 确定性的 Inbox 输入消息 ID。
        status: 执行实例状态，投递成功不直接改为 running。
        dispatch_failed_at: 最近一次投递失败时间。
        dispatch_error_code: 投递失败诊断代码。
        dispatch_error_message: 投递失败诊断说明。
        runtime_run_id: 开始执行后绑定的 Runtime run。
        started_at: Runtime 开始执行时间。
        completed_at: Runtime 终态时间，非终态必须为空。
        final_message_id: 最终回复消息 ID，允许为空。
        error_code: 执行失败代码，failed 状态必填。
        error_message: 执行失败说明，failed 状态必填。
        created_at: 执行实例物化时间，也是历史排序键。
        updated_at: 最近状态或诊断更新时间。
    """

    schedule_run_id: Identifier
    task_id: Identifier
    occurrence_key: NonEmptyText
    trigger_kind: ScheduleTriggerKind
    scheduled_for: AwareDatetime
    task_name_snapshot: NonEmptyText
    app_id_snapshot: Identifier
    user_id_snapshot: Identifier
    agent_id_snapshot: Identifier
    prompt_snapshot: NonEmptyText
    session_id: Identifier
    message_id: Identifier
    status: ScheduleRunStatus
    dispatch_failed_at: AwareDatetime | None = None
    dispatch_error_code: Identifier | None = None
    dispatch_error_message: NonEmptyText | None = None
    runtime_run_id: Identifier | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    final_message_id: Identifier | None = None
    error_code: Identifier | None = None
    error_message: NonEmptyText | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_state_fields(self) -> Self:
        """校验 Run 状态与 Runtime 关联字段。

        Returns:
            校验通过的当前模型实例。

        Raises:
            ValueError: 字段组合不满足模型约束。
        """
        terminal = self.status in {
            ScheduleRunStatus.SUCCEEDED,
            ScheduleRunStatus.FAILED,
            ScheduleRunStatus.CANCELED,
        }
        if self.status is not ScheduleRunStatus.PENDING:
            if self.runtime_run_id is None:
                raise ValueError("running/terminal Run 必须提供 runtime_run_id")
            if self.started_at is None:
                raise ValueError("running/terminal Run 必须提供 started_at")
        if terminal and self.completed_at is None:
            raise ValueError("terminal Run 必须提供 completed_at")
        if not terminal and self.completed_at is not None:
            raise ValueError("非终态 Run 不能提供 completed_at")
        if self.status is ScheduleRunStatus.FAILED and (
            self.error_code is None or self.error_message is None
        ):
            raise ValueError("failed Run 必须提供 error_code 和 error_message")
        if self.status is ScheduleRunStatus.SUCCEEDED and (
            self.error_code is not None or self.error_message is not None
        ):
            raise ValueError("succeeded Run 不能提供错误")
        return self


type MutableScheduleTaskStatus = Literal[
    ScheduleTaskStatus.ENABLED,
    ScheduleTaskStatus.DISABLED,
]


class ScheduleTaskPatch(StrictModel):
    """可用于未来 occurrence 的任务修改。

    Attributes:
        name: 未来触发使用的名称。
        prompt: 未来触发使用的输入文本。
        cron_expression: 新的五段 Cron。
        task_type: 新的周期或一次性类型。
        session_mode: 新的会话策略，固定会话由 Service 绑定。
        status: 只允许显式启用或停用。
    """

    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    prompt: NonEmptyText | None = None
    cron_expression: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    task_type: ScheduleTaskType | None = None
    session_mode: ScheduleSessionMode | None = None
    status: MutableScheduleTaskStatus | None = None

    @model_validator(mode="after")
    def require_change(self) -> Self:
        """拒绝没有任何显式字段的 patch。

        Returns:
            校验通过的当前模型实例。

        Raises:
            ValueError: 字段组合不满足模型约束。
        """
        if not self.model_fields_set:
            raise ValueError("定时任务修改至少一个字段")
        return self


class ScheduleScope(FrozenModel):
    """V1 中的 app + user + agent 查询范围。

    Attributes:
        app_id: 应用命名空间。
        user_id: 任务所属用户。
        agent_id: Agent ID。
    """

    app_id: Identifier
    user_id: Identifier
    agent_id: Identifier


class ScheduleTaskCursor(FrozenModel):
    """Task 键集分页游标。

    Attributes:
        updated_at: 上一页末项的修改时间。
        task_id: 时间相同时的稳定排序边界。
    """

    updated_at: AwareDatetime
    task_id: Identifier


class ScheduleRunCursor(FrozenModel):
    """Run 键集分页游标。

    Attributes:
        created_at: 上一页末项的创建时间。
        schedule_run_id: 时间相同时的稳定排序边界。
    """

    created_at: AwareDatetime
    schedule_run_id: Identifier


class ScheduleTaskQuery(StrictModel):
    """Task 列表查询。

    Attributes:
        status: 任务状态筛选，None 表示不筛选。
        task_type: 任务类型筛选，None 表示不筛选。
        cursor: 降序分页边界，None 表示首页。
        limit: 单页数量，范围为 1–100。
    """

    status: ScheduleTaskStatus | None = None
    task_type: ScheduleTaskType | None = None
    cursor: ScheduleTaskCursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class ScheduleRunQuery(StrictModel):
    """Run 历史查询。

    Attributes:
        task_id: 来源任务筛选，None 表示所有任务。
        status: 执行状态筛选，None 表示不筛选。
        cursor: 降序分页边界，None 表示首页。
        limit: 单页数量，范围为 1–100。
    """

    task_id: Identifier | None = None
    status: ScheduleRunStatus | None = None
    cursor: ScheduleRunCursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class ScheduleTaskPage(FrozenModel):
    """Task 键集分页结果。

    Attributes:
        items: 按修改时间与 ID 降序排列的任务。
        next_cursor: 下一页边界，无更多记录时为 None。
    """

    items: tuple[ScheduleTask, ...]
    next_cursor: ScheduleTaskCursor | None = None


class ScheduleRunPage(FrozenModel):
    """Run 键集分页结果。

    Attributes:
        items: 按创建时间与 ID 降序排列的执行实例。
        next_cursor: 下一页边界，无更多记录时为 None。
    """

    items: tuple[ScheduleRun, ...]
    next_cursor: ScheduleRunCursor | None = None


class MaterializeResult(FrozenModel):
    """唯一 occurrence 物化结果。

    Attributes:
        run: 新创建或已存在的唯一执行实例。
        was_created: 本次是否创建成功；只有创建者可以投递。
    """

    run: ScheduleRun
    was_created: bool


class ManualRunResult(FrozenModel):
    """手动立即执行的对话返回值。

    Attributes:
        run: 手动物化得到的执行实例。
        dispatch_result: 本次投递或 occurrence 去重结果。
    """

    run: ScheduleRun
    dispatch_result: DispatchResult


__all__ = [
    "DispatchResult",
    "ManualRunResult",
    "MaterializeResult",
    "MutableScheduleTaskStatus",
    "ScheduleRun",
    "ScheduleRunCursor",
    "ScheduleRunPage",
    "ScheduleRunQuery",
    "ScheduleRunStatus",
    "ScheduleScope",
    "ScheduleSessionMode",
    "ScheduleTask",
    "ScheduleTaskCursor",
    "ScheduleTaskPage",
    "ScheduleTaskPatch",
    "ScheduleTaskQuery",
    "ScheduleTaskStatus",
    "ScheduleTaskType",
    "ScheduleTriggerKind",
]
