"""当前进程活动 Run Task 的精确注册表。"""

import asyncio
from dataclasses import dataclass
from typing import Any

from agentsystem.common.models import SessionContext

type _RunTaskKey = tuple[str, str, str, str, str]


@dataclass(slots=True)
class _RunTaskEntry:
    """一个活动 run 的 Task 及用户取消归属。"""

    task: asyncio.Task[Any]
    user_requested: bool = False
    cancel_delivered: bool = False
    user_consumed: bool = False


class RunTaskRegistry:
    """保存本节点活动 run，并把广播转换成一次 Task 取消。

    以完整会话作用域和 run_id 定位，不管理 Task 的创建和等待。执行边界负责
    register/clear 配对，并消费用户取消标记以区分宿主停机等其他取消来源。
    """

    def __init__(self) -> None:
        """创建空注册表。"""
        self._entries: dict[_RunTaskKey, _RunTaskEntry] = {}

    def register(
        self,
        ctx: SessionContext,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        """注册已确定身份的活动 Task，不启动或等待它。

        Args:
            ctx: 完整会话作用域。
            run_id: 非空内部 run 标识。
            task: 当前执行该 run 的 asyncio Task。

        Raises:
            ValueError: run_id 为空。
            RuntimeError: 同一 run 已有未完成 Task。
        """
        key = _key(ctx, run_id)
        current = self._entries.get(key)
        if current is not None and not current.task.done():
            raise RuntimeError("同一 run 已经注册活动 Task")
        self._entries[key] = _RunTaskEntry(task=task)

    def request_if_active(self, ctx: SessionContext, run_id: str) -> bool:
        """命中活动 run 时记录用户取消，并至多调用一次 Task.cancel。

        Args:
            ctx: 完整会话作用域。
            run_id: 非空目标 run 标识。

        Returns:
            命中未完成 Task 时为 True，包括重复请求；不表示已收敛终态。
        """
        entry = self._entries.get(_key(ctx, run_id))
        if entry is None or entry.task.done():
            return False
        entry.user_requested = True
        if not entry.cancel_delivered:
            entry.cancel_delivered = True
            entry.task.cancel()
        return True

    def is_user_requested(self, ctx: SessionContext, run_id: str) -> bool:
        """查询注册项是否已收到用户取消，不改变标记。

        Args:
            ctx: 完整会话作用域。
            run_id: 非空目标 run 标识。

        Returns:
            注册项存在且已标记用户取消时为 True。
        """
        entry = self._entries.get(_key(ctx, run_id))
        return entry is not None and entry.user_requested

    def consume_user_cancellation(
        self,
        ctx: SessionContext,
        run_id: str,
    ) -> bool:
        """至多一次领取 Registry 发出的用户取消归属。

        Args:
            ctx: 完整会话作用域。
            run_id: 非空目标 run 标识。

        Returns:
            首次消费已请求的用户取消时为 True；不检查当前 Task 异常。
        """
        entry = self._entries.get(_key(ctx, run_id))
        if entry is None or not entry.user_requested or entry.user_consumed:
            return False
        entry.user_consumed = True
        return True

    def clear(self, ctx: SessionContext, run_id: str) -> None:
        """删除精确 run 的本地注册；不存在时无操作，不取消 Task。

        Args:
            ctx: 完整会话作用域。
            run_id: 非空目标 run 标识。
        """
        self._entries.pop(_key(ctx, run_id), None)


def _key(ctx: SessionContext, run_id: str) -> _RunTaskKey:
    """构造不会跨租户、Agent、用户或 session 冲突的 key。"""
    if not run_id:
        raise ValueError("run_id 不能为空")
    return (ctx.app_id, ctx.agent_id, ctx.user_id, ctx.session_id, run_id)
