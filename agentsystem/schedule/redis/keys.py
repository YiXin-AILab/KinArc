"""Schedule 模块独占的 Redis key 构造器。"""


class ScheduleRedisKeys:
    """构造 Schedule 私有 Redis 键，不执行 I/O。

    所有键共享 hash tag，供跨键 Lua 在一个 Cluster slot 中执行。

    Attributes:
        tasks_data: 保存 Task 正文的 Hash 键。
        tasks_enabled: 启用 Task 的扫描索引键。
        runs_data: 保存 ScheduleRun 正文的 Hash 键。
        occurrences: 任务与 occurrence 到 Run 的唯一性索引键。
    """

    def __init__(self, prefix: str) -> None:
        """绑定 Application Redis namespace。

        Args:
            prefix: Application 分配的 Redis 命名空间。
        """
        base = f"{prefix}:{{schedule}}"
        self.tasks_data = f"{base}:tasks:data"
        self.tasks_enabled = f"{base}:tasks:enabled"
        self.runs_data = f"{base}:runs:data"
        self.occurrences = f"{base}:runs:occurrences"


__all__ = ["ScheduleRedisKeys"]
