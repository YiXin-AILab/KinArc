"""五段 Cron 匹配与定时任务扫描。"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from agentsystem.schedule.manager import ScheduleManager
from agentsystem.schedule.models import (
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleSessionMode,
    ScheduleTask,
    ScheduleTriggerKind,
)
from agentsystem.schedule.service import ScheduleService

_FORBIDDEN_TOKENS = ("?", "L", "W", "#")


class CronSchedule:
    """按部署级 IANA 时区实现五段 Cron 匹配。

    封装 croniter，拒绝秒、年份、别名和 Quartz 扩展；日期与星期
    采用 OR 语义。实例不保存任务或启动后台工作，可被服务和扫描器共享。
    """

    def __init__(self, timezone: str) -> None:
        """初始化 Cron 匹配器。

        Args:
            timezone: IANA 时区名称。
        """
        self._timezone = ZoneInfo(timezone)

    def validate(self, expression: str) -> None:
        """校验标准五段 Cron 表达式。

        Args:
            expression: Cron 表达式。

        Raises:
            ValueError: 表达式不属于 V1 五段语法。
        """
        fields = expression.split()
        normalized = expression.upper()
        if (
            len(fields) != 5
            or expression.startswith("@")
            or any(token in normalized for token in _FORBIDDEN_TOKENS)
            or not croniter.is_valid(expression)
        ):
            raise ValueError("只允许标准五段 Cron")

    def matches(self, expression: str, now: datetime) -> bool:
        """判断表达式是否命中系统时区的当前分钟。

        Args:
            expression: Cron 表达式。
            now: 带时区的当前时间。

        Returns:
            当前分钟是否命中。
        """
        self.validate(expression)
        return croniter.match(
            expression,
            self._local_minute(now),
            day_or=True,
        )

    def scheduled_for(self, now: datetime) -> datetime:
        """返回当前系统分钟对应的 UTC 时间。

        Args:
            now: 带时区的当前时间。

        Returns:
            截断到分钟的 UTC 时间。
        """
        return self._local_minute(now).astimezone(UTC)

    def _local_minute(self, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Cron 时间必须包含时区")
        return value.astimezone(self._timezone).replace(second=0, microsecond=0)


def scheduled_occurrence_key(scheduled_for: datetime) -> str:
    """为自动触发分钟构造稳定 occurrence key。

    Args:
        scheduled_for: 带时区的计划时间。

    Returns:
        以 UTC 分钟表示的 occurrence key。

    Raises:
        ValueError: 时间不含时区。
    """
    if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
        raise ValueError("scheduled_for 必须包含时区")
    minute = scheduled_for.astimezone(UTC).replace(second=0, microsecond=0)
    return f"scheduled:{minute.strftime('%Y-%m-%dT%H:%MZ')}"


class ScheduleCoordinator:
    """串行扫描启用任务并投递新物化执行实例的后台服务。

    依赖共享 Manager、Cron 匹配器和 ScheduleService；时钟与 ID 工厂
    可注入。多节点依靠 occurrence 唯一性竞争，只有物化胜者投递。
    run 由宿主监督，close 唤醒间隔等待并停止后续循环，不取消当前扫描。
    """

    def __init__(
        self,
        *,
        manager: ScheduleManager,
        cron: CronSchedule,
        service: ScheduleService,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        scan_batch_size: int,
        scan_interval_seconds: float,
    ) -> None:
        """初始化可被 ServiceHost 监督的扫描器。

        Args:
            manager: 唯一权威 ScheduleManager。
            cron: 部署时区下的 Cron 校验与匹配器。
            service: Application 装配的共享业务服务。
            clock: 可注入的当前时间函数。
            id_factory: 生成 Task、Run 或独立会话 ID 的工厂。
            scan_batch_size: 每批扫描的启用任务数量。
            scan_interval_seconds: 每轮扫描结束后的等待秒数。
        """
        self._manager = manager
        self._cron = cron
        self._service = service
        self._clock = clock
        self._id_factory = id_factory
        self._scan_batch_size = scan_batch_size
        self._scan_interval_seconds = scan_interval_seconds
        self._closed = asyncio.Event()

    async def scan_once(self) -> None:
        """按 ID 分批遍历启用任务，整轮使用同一个当前分钟。

        命中任务先竞争唯一 occurrence，只有新建者调用 Service 投递。
        存储或匹配异常向宿主传播；本方法不补扫遗漏的历史分钟。
        """
        now = self._clock()
        scheduled_for = self._cron.scheduled_for(now)
        after_task_id: str | None = None
        while True:
            tasks = await self._manager.scan_enabled_tasks(
                after_task_id=after_task_id,
                limit=self._scan_batch_size,
            )
            if not tasks:
                return
            for task in tasks:
                if self._cron.matches(task.cron_expression, now):
                    await self._materialize_and_dispatch(task, scheduled_for)
            after_task_id = tasks[-1].task_id

    async def run(self) -> None:
        """串行运行扫描周期，不重叠 tick。

        扫描结束后等待配置间隔，close 可提前唤醒等待；扫描异常和取消
        传播给监督宿主，不在本层启动重试循环。
        """
        while not self._closed.is_set():
            await self.scan_once()
            try:
                await asyncio.wait_for(
                    self._closed.wait(),
                    timeout=self._scan_interval_seconds,
                )
            except TimeoutError:
                continue

    def close(self) -> None:
        """停止后续扫描并中断间隔等待。"""
        self._closed.set()

    async def _materialize_and_dispatch(
        self,
        task: ScheduleTask,
        scheduled_for: datetime,
    ) -> None:
        run = self._scheduled_run(task, scheduled_for)
        result = await self._manager.materialize_scheduled(
            candidate=task,
            run=run,
        )
        if result.was_created:
            await self._service.dispatch_run(result.run)

    def _scheduled_run(
        self,
        task: ScheduleTask,
        scheduled_for: datetime,
    ) -> ScheduleRun:
        run_id = self._id_factory()
        session_id = (
            task.fixed_session_id
            if task.session_mode is ScheduleSessionMode.FIXED
            else self._id_factory()
        )
        if session_id is None:
            raise ValueError("fixed 任务缺少会话")
        return ScheduleRun(
            schedule_run_id=run_id,
            task_id=task.task_id,
            occurrence_key=scheduled_occurrence_key(scheduled_for),
            trigger_kind=ScheduleTriggerKind.SCHEDULED,
            scheduled_for=scheduled_for,
            task_name_snapshot=task.name,
            app_id_snapshot=task.app_id,
            user_id_snapshot=task.user_id,
            agent_id_snapshot=task.agent_id,
            prompt_snapshot=task.prompt,
            session_id=session_id,
            message_id=f"schedule-run:{run_id}",
            status=ScheduleRunStatus.PENDING,
            created_at=scheduled_for,
            updated_at=scheduled_for,
        )


__all__ = ["CronSchedule", "ScheduleCoordinator", "scheduled_occurrence_key"]
