"""供显式启用工具使用的自有 Docker 沙箱，不依赖 Runtime 或评测模块。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

from agentsystem.common.errors import ToolError
from application.evoagentbench_knowledge_agent.plugins.execution import CommandResult


async def _read_bounded(stream: asyncio.StreamReader) -> tuple[str, bool]:
    data = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        room = 64000 - len(data)
        data.extend(chunk[:room])
        truncated |= len(chunk) > room
    return data.decode("utf-8", errors="replace"), truncated


async def docker_call(arguments: list[str], timeout: float) -> CommandResult:
    """以有界内存读取两路输出，中断时终止 CLI 进程。

    Args:
        arguments: Docker CLI 参数，不包含可执行文件名。
        timeout: 命令执行及两路输出读取的总时长上限，单位为秒。

    Returns:
        退出码与 UTF-8 输出，并标记任一路输出是否被截断。

    Raises:
        TimeoutError: Docker CLI 或输出读取超过时限。
        asyncio.CancelledError: 调用方取消了当前调用。
    """
    child = await asyncio.create_subprocess_exec(
        "docker",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert child.stdout is not None and child.stderr is not None
    readers = [
        asyncio.create_task(_read_bounded(s)) for s in (child.stdout, child.stderr)
    ]
    try:
        async with asyncio.timeout(timeout):
            await child.wait()
            output, error = await asyncio.gather(*readers)
        return CommandResult(
            exit_code=child.returncode or 0,
            stdout=output[0],
            stderr=error[0],
            truncated=output[1] or error[1],
        )
    finally:
        if child.returncode is None:
            with suppress(ProcessLookupError):
                child.kill()
            await child.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


class DockerEnvironment:
    """每个持有者使用独立容器，超时会使环境失效。"""

    def __init__(self, image: str, *, name: str | None = None) -> None:
        """预留持有者身份，不创建容器。

        Args:
            image: 已安装的本地镜像名称或不可变镜像 ID。
            name: 可选容器名称；未指定时生成独立名称。
        """
        self.image = image
        self.name = name or f"agent-tools-{uuid4().hex}"
        self._owner = uuid4().hex
        self._started = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """创建非特权沙箱，不隐式拉取镜像。

        Raises:
            ValueError: 当前实例已持有启动的执行环境。
            ToolError: Docker 无法创建或启动任务容器。
        """
        if self._started:
            raise ValueError("Environment already started")
        # 先创建再启动，使取消后的清理能通过预留名称定位容器。
        try:
            result = await self._create()
        except (TimeoutError, asyncio.CancelledError):
            self._started = True
            await self.close()
            raise
        if result.exit_code:
            raise ToolError(code="environment_create_failed", message=result.stderr)
        self._started = True
        try:
            result = await docker_call(["start", self.name], 30)
            if result.exit_code:
                raise ToolError(code="environment_start_failed", message=result.stderr)
        except BaseException:
            await self.close()
            raise

    async def _create(self) -> CommandResult:
        return await docker_call(
            [
                "create",
                "--pull=never",
                "--name",
                self.name,
                f"--label=agentsystem.owner={self._owner}",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=128",
                "--memory=1g",
                "--cpus=1",
                "--user=65534:65534",
                # 旧版 Docker 会在 tmpfs 挂载上恢复镜像目录的权限。
                # 显式设置归属，确保两个挂载目录均可由任务用户写入。
                "--tmpfs=/workspace:rw,exec,size=256m,mode=1777,uid=65534,gid=65534",
                "--tmpfs=/tmp:rw,exec,size=128m,mode=1777,uid=65534,gid=65534",
                "--workdir=/workspace",
                "--env=HOME=/tmp",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint=/bin/sh",
                self.image,
                "-c",
                "while :; do sleep 3600; done",
            ],
            30,
        )

    async def run(
        self, command: str, cwd: str, timeout_seconds: float
    ) -> CommandResult:
        """串行处理修改操作，超时或取消时终止自有容器。

        Args:
            command: 仅在自有任务容器中执行的 shell 命令。
            cwd: 当前容器内的工作目录。
            timeout_seconds: Docker 命令执行时长上限，单位为秒。

        Returns:
            有大小限制的命令输出；超时清理成功后返回退出码 124。

        Raises:
            ToolError: 环境已关闭，或容器清理失败。
            asyncio.CancelledError: 调用被取消，且清理成功。
        """
        async with self._lock:
            if not self._started:
                raise ToolError(
                    code="environment_closed", message="Task environment is closed"
                )
            try:
                return await docker_call(
                    ["exec", "--workdir", cwd, self.name, "/bin/sh", "-c", command],
                    timeout_seconds,
                )
            except TimeoutError:
                await self.close()
                return CommandResult(
                    exit_code=124,
                    stdout="",
                    stderr="Command timed out; task environment closed",
                    timed_out=True,
                )
            except asyncio.CancelledError:
                await self.close()
                raise

    async def close(self) -> None:
        """仅删除当前持有的容器，并传播清理失败。

        容器已不存在时视为关闭；删除前校验持有者标签，
        防止名称复用导致误删其他调用方的环境。

        Raises:
            ToolError: 持有者校验、容器检查或容器删除失败。
        """
        if not self._started:
            return
        identity = await docker_call(
            [
                "inspect",
                "--format",
                '{{index .Config.Labels "agentsystem.owner"}}',
                self.name,
            ],
            30,
        )
        if identity.exit_code:
            if "no such" not in identity.stderr.lower():
                raise ToolError(
                    code="environment_inspect_failed", message=identity.stderr
                )
            self._started = False
            return
        if identity.stdout.strip() != self._owner:
            raise ToolError(
                code="environment_owner_mismatch",
                message="Refusing to remove another owner's container",
            )
        result = await docker_call(["rm", "-f", self.name], 30)
        if result.exit_code and "no such container" not in result.stderr.lower():
            raise ToolError(code="environment_cleanup_failed", message=result.stderr)
        self._started = False
