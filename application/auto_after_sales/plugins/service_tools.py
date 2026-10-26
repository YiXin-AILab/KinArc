"""汽车售后客服 Agent 业务插件：客户、预约、通知与招揽提醒工具。"""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from agentsystem.common.errors import ToolError
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.service.agents import ApplicationRoot
from agentsystem.tool.models import ToolOutput
from pydantic import Field

from application.auto_after_sales.services.customer_service import (
    Advisor,
    CustomerService,
)
from application.auto_after_sales.services.notify_service import NotifyService
from application.auto_after_sales.services.reminder_service import (
    ReminderService,
)


class ServiceBundle:
    """插件内共享的服务集合，便于测试以临时数据目录重建。"""

    def __init__(
        self,
        customers: CustomerService,
        notify: NotifyService,
        reminders: ReminderService,
    ) -> None:
        """聚合三个服务实例。

        Args:
            customers: 客户档案与预约服务。
            notify: 本地通知记录服务。
            reminders: 提醒草稿与审核服务。
        """
        self.customers = customers
        self.notify = notify
        self.reminders = reminders

    @classmethod
    def from_data_root(cls, data_root: Path) -> "ServiceBundle":
        """按应用 data 目录构建完整服务集合。"""
        customers = CustomerService(data_root)
        notify = NotifyService(data_root / "notifications.jsonl")
        return cls(
            customers=customers,
            notify=notify,
            reminders=ReminderService(
                customers,
                data_root / "reminders.json",
                notify,
            ),
        )


def register(context: PluginSetupContext) -> None:
    """注册汽车售后客服的全部业务 Tool。"""
    root_capability = context.get(ApplicationRoot)
    if root_capability is None:
        raise ValueError(f"Plugin {context.plugin_id} 缺少 ApplicationRoot capability")
    bundle = ServiceBundle.from_data_root(root_capability.path / "data")
    _register_customer_tools(context, bundle)
    _register_service_tools(context, bundle)
    _register_reminder_tools(context, bundle)


def _register_customer_tools(
    context: PluginSetupContext,
    bundle: ServiceBundle,
) -> None:
    """注册客户档案与预约类 Tool。"""

    def customer_lookup(
        query: Annotated[
            str,
            Field(description="姓名、手机号或车架号（VIN）片段，支持模糊匹配"),
        ],
    ) -> ToolOutput:
        """按姓名/手机号/VIN 查询客户档案，返回车型、在保状态与专属顾问。"""
        matches = bundle.customers.find_customers(query)
        if not matches:
            return ToolOutput(content=f"未找到匹配「{query}」的客户档案。")
        lines = [
            (
                f"{item.name}（{item.customer_id}）：车型 {item.model}，"
                "联系方式已匹配档案，购车 "
                f"{item.purchase_date.isoformat()}，上次保养 "
                f"{item.last_service_date.isoformat()} / "
                f"{item.last_service_mileage} 公里，车险到期 "
                f"{item.insurance_expiry.isoformat()}，专属顾问 "
                f"{item.preferred_advisor}，标签：{','.join(item.tags) or '无'}"
            )
            for item in matches
        ]
        return ToolOutput(content="\n".join(lines))

    def create_appointment(
        customer_id: Annotated[
            str,
            Field(description="客户档案 ID，形如 cust-001，可由 customer_lookup 获得"),
        ],
        service_type: Annotated[
            str,
            Field(description="服务类型，如 A保、B保、维修检测、钣喷、续保"),
        ],
        date: Annotated[
            str,
            Field(description="预约到店日期，YYYY-MM-DD"),
        ],
        time_slot: Annotated[
            str,
            Field(description="到店时段，如 09:00-10:00"),
        ],
        note: Annotated[
            str,
            Field(description="备注，如取送车需求、客户特殊要求"),
        ] = "",
    ) -> ToolOutput:
        """为客户登记到店保养/维修预约。"""
        try:
            start, end = time_slot.split("-")
            zone = ZoneInfo("Asia/Shanghai")
            planned = datetime.fromisoformat(f"{date}T{start}").replace(tzinfo=zone)
            finish = datetime.fromisoformat(f"{date}T{end}").replace(tzinfo=zone)
            if planned <= datetime.now(zone) or planned >= finish:
                raise ValueError("past appointment")
        except ValueError as error:
            raise ToolError(
                code="appointment_time_invalid",
                message="预约日期或时段无效，或早于当前时间。请以本轮current-datetime注入的时间核对日期，再按客户原意重新登记。",
            ) from error
        try:
            appointment = bundle.customers.create_appointment(
                customer_id=customer_id,
                service_type=service_type,
                date=date,
                time_slot=time_slot,
                note=note,
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
        except ValueError as error:
            raise ToolError(
                code="customer_not_found",
                message=str(error),
            ) from error
        return ToolOutput(
            content=(
                f"预约登记成功：{appointment.appointment_id}，客户 "
                f"{appointment.customer_id}，{appointment.service_type}，"
                f"{appointment.date} {appointment.time_slot}。"
                "顾问将在到店前 1 天短信确认。"
            )
        )

    context.register_tool(customer_lookup)
    context.register_tool(create_appointment, is_concurrency_safe=False)


def _register_service_tools(
    context: PluginSetupContext,
    bundle: ServiceBundle,
) -> None:
    """注册服务类诉求与事故救急 Tool。"""

    def forward_to_advisor(
        summary: Annotated[
            str,
            Field(
                description=("服务类诉求要点总结，须包含客户核心诉求、已告知信息与期望")
            ),
        ],
        customer_id: Annotated[
            str,
            Field(description="可选，客户档案 ID，用于定位专属顾问"),
        ] = "",
        advisor_id: Annotated[
            str,
            Field(description="可选，指定顾问 ID；缺省时转客户专属顾问"),
        ] = "",
    ) -> ToolOutput:
        """把服务类诉求要点转发给内部顾问跟进。"""
        advisor_id = advisor_id or _preferred_advisor_id(bundle, customer_id)
        advisor = _find_advisor(bundle, advisor_id)
        if advisor is None:
            raise ToolError(
                code="advisor_not_found",
                message=f"顾问不存在：{advisor_id or '（未指定且无专属顾问）'}",
            )
        bundle.notify.forward_to_advisor(
            summary=summary,
            advisor_id=advisor.advisor_id,
            advisor_name=advisor.name,
            customer_id=customer_id or None,
        )
        return ToolOutput(
            content=(
                f"已转发给顾问 {advisor.name}（{advisor.specialty}，"
                f"{advisor.shift}），顾问会尽快联系客户。"
            )
        )

    def escalate_emergency(
        description: Annotated[
            str,
            Field(description="紧急情况描述：事故/自燃/抛锚现象与人员伤亡情况"),
        ],
        customer_id: Annotated[
            str,
            Field(description="可选，客户档案 ID，用于值班专员回电"),
        ] = "",
        location: Annotated[
            str,
            Field(description="可选，事发位置，便于救援调度"),
        ] = "",
    ) -> ToolOutput:
        """事故救急：立即通知 7×24 值班应急救援专员，不得延迟调用。"""
        oncall = bundle.customers.emergency_advisor()
        if oncall is None:
            raise ToolError(
                code="emergency_oncall_missing",
                message="未配置应急救援值班人员，请人工介入",
            )
        bundle.notify.escalate_emergency(
            description=description,
            oncall_advisor_id=oncall.advisor_id,
            oncall_name=oncall.name,
            oncall_phone=oncall.phone,
            customer_id=customer_id or None,
            location=location or None,
        )
        return ToolOutput(
            content=(
                f"已立即通知应急救援值班专员 {oncall.name}，专员将第一时间"
                "电话联系客户介入处理。请告知客户保持电话畅通、确保人身安全。"
            )
        )

    def file_complaint(
        category: Annotated[
            str,
            Field(description="投诉类别，如 服务态度/维修质量/收费争议/等待时长"),
        ],
        content: Annotated[
            str,
            Field(description="投诉具体内容与客户诉求"),
        ],
        customer_id: Annotated[
            str,
            Field(description="可选，客户档案 ID"),
        ] = "",
    ) -> ToolOutput:
        """登记客户投诉，承诺 24 小时内响应。"""
        bundle.notify.forward_to_advisor(
            summary=f"【投诉-{category}】{content}",
            advisor_id="adv-complaint-desk",
            advisor_name="客户关系部",
            customer_id=customer_id or None,
        )
        return ToolOutput(
            content=(
                "投诉已登记并转客户关系部，承诺 24 小时内首次响应、"
                "3 个工作日内给出处理方案。"
            )
        )

    def list_advisors(
        specialty: Annotated[
            str,
            Field(description="可选，按专长关键字过滤，如 机电/钣喷/续保/救援"),
        ] = "",
    ) -> ToolOutput:
        """查询服务顾问名单及其专长与班次。"""
        advisors = bundle.customers.list_advisors(specialty or None)
        if not advisors:
            return ToolOutput(content=f"未找到专长匹配「{specialty}」的顾问。")
        lines = [
            (
                f"{item.name}（{item.advisor_id}）：{item.specialty}，"
                f"{item.shift}，电话 {item.phone}"
                f"{'，应急救援值班' if item.emergency_oncall else ''}"
            )
            for item in advisors
        ]
        return ToolOutput(content="\n".join(lines))

    context.register_tool(forward_to_advisor)
    context.register_tool(escalate_emergency, is_concurrency_safe=False)
    context.register_tool(file_complaint)
    context.register_tool(list_advisors)


def _register_reminder_tools(
    context: PluginSetupContext,
    bundle: ServiceBundle,
) -> None:
    """注册招揽提醒生成与审核 Tool。"""

    def generate_reminders() -> ToolOutput:
        """扫描客户档案生成保养/生日/续保提醒草稿，供人工审核后发送。"""
        report = bundle.reminders.generate()
        if not report.created:
            detail = (
                f"；跳过 {len(report.skipped)} 条（30 天内已有同类型记录）"
                if report.skipped
                else ""
            )
            return ToolOutput(content=f"本次未产生新提醒草稿{detail}。")
        lines = [
            (
                f"{item['reminder_id']} [{item['type']}] {item['title']}："
                f"{item['reason']}"
            )
            for item in report.created
        ]
        summary = f"本次生成 {len(report.created)} 条提醒草稿（待审核）"
        if report.skipped:
            summary += f"，频控跳过 {len(report.skipped)} 条"
        return ToolOutput(content=summary + "：\n" + "\n".join(lines))

    def list_reminders(
        status: Annotated[
            Literal["pending", "sent", "dismissed"],
            Field(
                description=("按状态过滤：pending 待审核/sent 已发送/dismissed 已驳回")
            ),
        ] = "pending",
    ) -> ToolOutput:
        """列出客户提醒草稿，默认列出待审核的。"""
        records = bundle.reminders.list_reminders(status)
        if not records:
            return ToolOutput(content=f"没有状态为 {status} 的提醒。")
        lines = [
            (
                f"{item['reminder_id']} [{item['type']}] {item['title']}\n"
                f"  内容：{item['content']}\n"
                f"  依据：{item['reason']}"
            )
            for item in records
        ]
        return ToolOutput(content="\n".join(lines))

    def review_reminder(
        reminder_id: Annotated[
            str,
            Field(description="提醒 ID，形如 rem-cust-001-maintenance-20260930"),
        ],
        action: Annotated[
            Literal["send", "dismiss"],
            Field(description="审核动作：send 发送给客户 / dismiss 驳回"),
        ],
    ) -> ToolOutput:
        """审核提醒草稿：发送（触达客户）或驳回。"""
        try:
            bundle.reminders.review(reminder_id, action)
        except ValueError as error:
            raise ToolError(
                code="reminder_review_failed",
                message=str(error),
            ) from error
        if action == "send":
            return ToolOutput(content=f"提醒 {reminder_id} 已通过审核并发送给客户。")
        return ToolOutput(content=f"提醒 {reminder_id} 已驳回。")

    context.register_tool(generate_reminders, is_concurrency_safe=False)
    context.register_tool(list_reminders)
    context.register_tool(review_reminder, is_concurrency_safe=False)


def _preferred_advisor_id(bundle: ServiceBundle, customer_id: str) -> str:
    if not customer_id:
        return ""
    customer = bundle.customers.get_customer(customer_id)
    return customer.preferred_advisor if customer is not None else ""


def _find_advisor(bundle: ServiceBundle, advisor_id: str) -> Advisor | None:
    if not advisor_id:
        return None
    for advisor in bundle.customers.list_advisors():
        if advisor.advisor_id == advisor_id:
            return advisor
    return None
