"""定时任务时间与权威存储协议。"""

from datetime import datetime
from typing import Protocol

from agentsystem.schedule.models import (
    MaterializeResult,
    ScheduleRun,
    ScheduleRunPage,
    ScheduleRunQuery,
    ScheduleRunStatus,
    ScheduleScope,
    ScheduleTask,
    ScheduleTaskPage,
    ScheduleTaskPatch,
    ScheduleTaskQuery,
)


class CronMatcher(Protocol):
    """定时任务的五段 Cron 校验和分钟匹配协议。

    实现负责部署时区与 UTC 分钟的转换，领域存储只依赖此边界，
    无需导入第三方 Cron 库；不维护计时任务或调度状态。
    """

    def validate(self, expression: str) -> None:
        """校验 Cron 表达式。

        Args:
            expression: 标准五段 Cron 表达式。
        """
        ...

    def matches(self, expression: str, now: datetime) -> bool:
        """判断 Cron 是否命中指定时刻所在分钟。

        Args:
            expression: 标准五段 Cron 表达式。
            now: 带时区的当前时间。

        Returns:
            表达式是否命中部署时区中的当前分钟。
        """
        ...

    def scheduled_for(self, now: datetime) -> datetime:
        """得到指定时刻所在的 UTC 分钟。

        Args:
            now: 带时区的当前时间。

        Returns:
            指定时刻所在分钟对应的 UTC 时间。
        """
        ...


class ScheduleManager(Protocol):
    """ScheduleTask 与 ScheduleRun 的唯一权威存储协议。

    任务修改通过版本 CAS，执行实例通过任务与 occurrence 唯一性
    原子物化；仅物化胜者可以投递 Inbox。存储不执行 Agent 或投递消息，
    不管理 Runtime run 生命周期，只接受 Service/Middleware 的状态更新。
    """

    async def create_task(self, task: ScheduleTask) -> ScheduleTask:
        """创建任务。

        Args:
            task: 完整的任务事实。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务 ID 已存在或存储操作失败。
        """
        ...

    async def get_task(
        self,
        scope: ScheduleScope,
        task_id: str,
    ) -> ScheduleTask | None:
        """按作用域读取任务。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。

        Returns:
            范围内未删除的任务；不存在、已删除或越界时为 None。
        """
        ...

    async def list_tasks(
        self,
        scope: ScheduleScope,
        query: ScheduleTaskQuery,
    ) -> ScheduleTaskPage:
        """分页查询任务。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按修改时间与 ID 降序排列的任务页及下一页边界。
        """
        ...

    async def scan_enabled_tasks(
        self,
        *,
        after_task_id: str | None,
        limit: int,
    ) -> tuple[ScheduleTask, ...]:
        """以 task ID 升序扫描可触发任务。

        Args:
            after_task_id: 上一批最后一个 Task ID，None 表示从头扫描。
            limit: 本次最多返回的记录数量。

        Returns:
            按 Task ID 升序排列的启用任务批次，空元组表示扫描结束。
        """
        ...

    async def update_task(
        self,
        scope: ScheduleScope,
        task_id: str,
        *,
        expected_version: int,
        patch: ScheduleTaskPatch,
        updated_at: datetime,
        fixed_session_id: str | None,
    ) -> ScheduleTask:
        """通过 version CAS 修改任务。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。
            expected_version: 调用方读取的任务版本，用于拒绝过期修改。
            patch: 任务修改内容，只影响未来物化的执行实例。
            updated_at: 本次修改时间。
            fixed_session_id: 切换固定模式时绑定的会话 ID。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务不存在、版本冲突或存储操作失败。
        """
        ...

    async def delete_task(
        self,
        scope: ScheduleScope,
        task_id: str,
        *,
        expected_version: int,
        deleted_at: datetime,
    ) -> ScheduleTask:
        """通过 version CAS 软删除任务。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。
            expected_version: 调用方读取的任务版本，用于拒绝过期修改。
            deleted_at: 本次软删除时间。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务不存在、版本冲突或存储操作失败。
        """
        ...

    async def materialize_scheduled(
        self,
        *,
        candidate: ScheduleTask,
        run: ScheduleRun,
    ) -> MaterializeResult:
        """原子物化自动 occurrence。

        Args:
            candidate: 扫描时的 Task 快照，物化时需复核版本和状态。
            run: 执行实例及物化时的身份、输入快照。

        Returns:
            新建或既有的唯一实例，以及本调用是否拥有首次投递资格。

        Raises:
            AdapterError: 当前任务不再满足物化条件、唯一性冲突或存储失败。
        """
        ...

    async def materialize_manual(
        self,
        *,
        task: ScheduleTask,
        run: ScheduleRun,
    ) -> MaterializeResult:
        """原子物化手动 occurrence。

        Args:
            task: 完整的任务事实。
            run: 执行实例及物化时的身份、输入快照。

        Returns:
            新建或既有的唯一实例，以及本调用是否拥有首次投递资格。

        Raises:
            AdapterError: 任务已删除、实例状态冲突或存储失败。
        """
        ...

    async def record_dispatch_failure(
        self,
        schedule_run_id: str,
        *,
        failed_at: datetime,
        error_code: str,
        error_message: str,
    ) -> None:
        """尽力记录 MQ 投递失败诊断。

        Args:
            schedule_run_id: 目标 ScheduleRun ID。
            failed_at: 本次 Inbox 投递失败时间。
            error_code: 失败诊断代码。
            error_message: 失败诊断说明。

        Raises:
            AdapterError: 执行实例不存在、不处于 pending 或存储失败。
        """
        ...

    async def mark_runs_running(
        self,
        schedule_run_ids: tuple[str, ...],
        *,
        runtime_run_id: str,
        started_at: datetime,
    ) -> None:
        """批量绑定 Runtime run 并进入 running。

        Args:
            schedule_run_ids: 需要绑定到同一 Runtime run 的执行实例 ID。
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            started_at: Runtime 实际开始时间。

        Raises:
            AdapterError: 实例不存在、状态或 Runtime 绑定冲突，或存储失败。
        """
        ...

    async def mark_runs_terminal(
        self,
        runtime_run_id: str,
        *,
        status: ScheduleRunStatus,
        completed_at: datetime,
        final_message_id: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """批量收敛共享 Runtime run 的 ScheduleRun。

        Args:
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            status: 目标 Runtime 终态，只允许成功、失败或取消。
            completed_at: Runtime 终态时间。
            final_message_id: 最终回复消息 ID，无回复时为 None。
            error_code: 失败诊断代码。
            error_message: 失败诊断说明。

        Raises:
            AdapterError: 终态无效、状态冲突或存储失败。
        """
        ...

    async def list_runs(
        self,
        scope: ScheduleScope,
        query: ScheduleRunQuery,
    ) -> ScheduleRunPage:
        """分页查询执行历史。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按创建时间与 ID 降序排列的历史页及下一页边界。
        """
        ...


__all__ = ["CronMatcher", "ScheduleManager"]
