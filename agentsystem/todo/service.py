"""Todo 内容服务，不依赖会话状态或消息历史。"""

from agentsystem.common.models import SessionContext
from agentsystem.todo.models import TodoItem, validate_identity, validate_todos
from agentsystem.todo.store import TodoStore


class TodoService:
    """工具、提醒和查询共享的 Todo 内容入口。

    依赖 Application 注入的 TodoStore；在存储调用前验证会话身份，
    替换前重验完整清单。服务不缓存内容，不依赖消息历史或会话状态，
    也不因 Runtime 结束而自动完成或清空条目。
    """

    def __init__(self, store: TodoStore) -> None:
        """绑定 Application 管理的存储。

        Args:
            store: Application 共享的权威存储，不转移生命周期所有权。
        """
        self._store = store

    async def read(self, ctx: SessionContext) -> tuple[TodoItem, ...]:
        """读取当前身份下的清单。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。

        Returns:
            按原顺序返回的完整清单，不存在或已清空时为空。
        """
        validate_identity(ctx)
        return await self._store.read(ctx)

    async def replace(
        self, ctx: SessionContext, todos: tuple[TodoItem, ...]
    ) -> tuple[TodoItem, ...]:
        """校验并完整替换，不判断会话状态。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            todos: 按展示顺序排列的完整清单，空清单表示清空。

        Returns:
            校验后的完整条目序列；写入方法返回本次提交内容。
        """
        validate_identity(ctx)
        return await self._store.replace(ctx, validate_todos(todos))
