"""Todo 唯一权威存储协议。"""

from typing import Protocol

from agentsystem.common.models import SessionContext
from agentsystem.todo.models import TodoItem


class TodoStore(Protocol):
    """按完整会话身份隔离的 Todo 权威存储协议。

    实现原子读取与整份替换，保留条目顺序，空元组表示清空。
    不查询会话状态、不执行条目、不进行逐项合并或状态迁移校验；
    实例和外部资源由 Application 管理。
    """

    async def read(self, ctx: SessionContext) -> tuple[TodoItem, ...]:
        """返回完整清单，不存在返回空序列。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。

        Returns:
            按原顺序返回的完整清单，不存在或已清空时为空。
        """
        ...

    async def replace(
        self, ctx: SessionContext, todos: tuple[TodoItem, ...]
    ) -> tuple[TodoItem, ...]:
        """一次提交完整清单，返回本次提交内容。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            todos: 按展示顺序排列的完整清单，空清单表示清空。

        Returns:
            校验后的完整条目序列；写入方法返回本次提交内容。
        """
        ...
