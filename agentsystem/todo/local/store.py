"""同一事件循环内无 await 临界区的 Todo 存储。"""

from agentsystem.common.models import SessionContext
from agentsystem.todo.models import TodoItem, validate_identity, validate_todos


class LocalTodoStore:
    """在实例内按完整会话身份保存 Todo 清单。

    读取和整份替换不包含 await 临界点，只保证同一事件循环内不交错。
    条目为不可变模型，数据随实例销毁而丢失，不提供跨进程持久化。
    """

    def __init__(self) -> None:
        """创建进程内映射。"""
        self._todos: dict[tuple[str, str, str, str], tuple[TodoItem, ...]] = {}

    async def read(self, ctx: SessionContext) -> tuple[TodoItem, ...]:
        """读取不可变清单，不存在时返回空序列。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。

        Returns:
            按原顺序返回的完整清单，不存在或已清空时为空。
        """
        return self._todos.get(validate_identity(ctx), ())

    async def replace(
        self, ctx: SessionContext, todos: tuple[TodoItem, ...]
    ) -> tuple[TodoItem, ...]:
        """校验后一次替换，无调度切换。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            todos: 按展示顺序排列的完整清单，空清单表示清空。

        Returns:
            校验后的完整条目序列；写入方法返回本次提交内容。
        """
        key = validate_identity(ctx)
        result = validate_todos(todos)
        self._todos[key] = result
        return result
