"""将工具绑定到可信的上游环境，不暴露评分数据。"""

import asyncio
import shlex

from application.evoagentbench_knowledge_agent.plugins.docker import docker_call
from application.evoagentbench_knowledge_agent.plugins.execution import CommandResult


class AttachedContainer:
    """使用办公任务容器，由上游持有者负责清理。"""

    def __init__(self, name: str) -> None:
        """连接调用方持有的容器，不创建或启动容器。

        Args:
            name: 上游 worker 提供的已有办公任务容器名称。
        """
        self.name = name
        self.closed = False

    async def run(
        self, command: str, cwd: str, timeout_seconds: float
    ) -> CommandResult:
        """执行有超时约束的命令，中断时将环境标记为失效。

        Args:
            command: 仅在已连接容器内执行的 shell 命令。
            cwd: 容器内的工作目录。
            timeout_seconds: 由容器内 timeout 工具强制执行的命令超时秒数。

        Returns:
            有大小限制的命令输出及退出码，包含超时退出码。

        Raises:
            RuntimeError: 环境已关闭，或容器失效处理失败。
            TimeoutError: Docker CLI 超过外层命令时限。
            asyncio.CancelledError: 调用被取消，且环境失效处理成功。
        """
        if self.closed:
            raise RuntimeError("Task container was invalidated by a timeout")
        try:
            result = await docker_call(
                [
                    "exec",
                    "--workdir",
                    cwd,
                    self.name,
                    "/bin/bash",
                    "-c",
                    f"exec timeout --kill-after=2s {timeout_seconds:g}s /bin/bash -c "
                    + shlex.quote(command),
                ],
                timeout_seconds + 5,
            )
            if result.exit_code in {124, 137}:
                await self._invalidate()
            return result
        except (TimeoutError, asyncio.CancelledError):
            await self._invalidate()
            raise

    async def _invalidate(self) -> None:
        self.closed = True
        result = await docker_call(["kill", self.name], 15)
        if result.exit_code:
            raise RuntimeError("Unable to stop timed-out task container")
