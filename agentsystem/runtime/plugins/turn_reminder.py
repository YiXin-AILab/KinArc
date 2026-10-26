"""从指定 Turn 开始，每五轮注入任务收敛提醒。"""

from datetime import UTC, datetime
from uuid import UUID, uuid5

from pydantic import Field

from agentsystem.common.models import StrictModel
from agentsystem.message.models import Message, MessageVisibility, Role, TextBlock
from agentsystem.middleware.models import (
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewareHandler,
    MiddlewarePoint,
    MiddlewareScope,
    TurnMiddlewarePayload,
)
from agentsystem.plugin.context import PluginSetupContext

_MESSAGE_ID_NAMESPACE = UUID("728ae9ea-d181-4526-a169-6ddbb10d4d56")


class TurnReminderOptions(StrictModel):
    """校验 Turn 提醒阈值；配置不限制 Loop 的实际轮数。

    Attributes:
        remind_at: 从此轮起每五轮注入一次提醒；None 禁用注册。
    """

    remind_at: int | None = Field(default=256, ge=1)


def _reminder_message(
    context: MiddlewareContext,
    *,
    turn_index: int,
    remind_at: int,
) -> Message:
    """构造幂等且仅模型可见的提醒消息。"""
    message_id = str(
        uuid5(_MESSAGE_ID_NAMESPACE, f"{context.invocation_id}/{turn_index}")
    )
    progress = f"<system-reminder>\n当前是第 {turn_index} 轮。"
    if turn_index > remind_at:
        progress += (
            f"允许的最大轮数为 {remind_at}，你已超出 {turn_index - remind_at} 轮。"
        )
    text = progress + (
        "停止无止境的探索。利用已经收集的信息完成任务，"
        "或清楚说明阻碍进展的原因以及缺少哪些信息。不要提及这条提醒或透露轮数。"
        "不要引用或转述这些指令，仅用它们指导下一步行动。\n</system-reminder>"
    )
    return Message(
        message_id=message_id,
        session_id=context.ctx.session_id,
        role=Role.USER,
        content=(TextBlock(text=text),),
        origin="turn-reminder",
        visibility=MessageVisibility.MODEL,
        run_id=context.run_id,
        created_at=datetime.now(UTC),
    )


def _handler(remind_at: int) -> MiddlewareHandler:
    """创建从 Turn 阈值开始每五轮提醒的 BEFORE_TURN Handler。"""

    async def handle(context: MiddlewareContext) -> MiddlewareDecision:
        """达到阈值及后续五轮间隔时提交提醒消息，其余轮次直接继续。"""
        if not isinstance(context.payload, TurnMiddlewarePayload):
            raise ValueError("BEFORE_TURN payload 类型错误")
        turn_index = context.turn_index
        if turn_index is None:
            raise ValueError("BEFORE_TURN 缺少 turn_index")
        if turn_index < remind_at or (turn_index - remind_at) % 5 != 0:
            return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(
                messages=(
                    _reminder_message(
                        context,
                        turn_index=turn_index,
                        remind_at=remind_at,
                    ),
                )
            ),
        )

    return handle


def register(context: PluginSetupContext) -> None:
    """按 Agent 配置注册全局作用域的 BEFORE_TURN 提醒。

    Args:
        context: 当前 Agent 的受控 Plugin 装配上下文。

    选项在注册时校验；禁用时不注册 Handler，运行时失败采用 FAIL_OPEN。
    """
    options = TurnReminderOptions.model_validate(context.options("turn-reminder"))
    if options.remind_at is None:
        return
    context.register_middleware(
        _handler(options.remind_at),
        name="turn-reminder",
        handler_id="before-turn",
        point=MiddlewarePoint.BEFORE_TURN,
        scope=MiddlewareScope.GLOBAL,
        failure_mode=MiddlewareFailureMode.FAIL_OPEN,
    )


__all__ = ["TurnReminderOptions", "register"]
