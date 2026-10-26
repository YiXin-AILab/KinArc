"""绑定到调用方持有执行环境的命令与文件工具。"""

import base64
import json
import math
import shlex
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Protocol

from agentsystem.common.errors import ToolError
from agentsystem.tool.builder import build_function_tool
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource


@dataclass(frozen=True)
class CommandResult:
    """有大小限制的命令输出；非零退出码对模型可见。"""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False


class ExecutionEnvironment(Protocol):
    """由应用负责执行环境的创建、隔离与清理。"""

    async def run(
        self, command: str, cwd: str, timeout_seconds: float
    ) -> CommandResult:
        """在已绑定的执行环境内执行命令，不隐式在宿主运行。

        Args:
            command: 在调用方持有的环境内执行的 shell 命令。
            cwd: 执行环境内的工作目录。
            timeout_seconds: 命令执行时长上限，单位为秒。

        Returns:
            有大小限制的输出与命令完成元数据。
        """
        ...


def relative_path(path: str) -> str:
    """在外部 I/O 前拒绝绝对路径和原始路径中的目录越界。

    Args:
        path: 相对于工作区的非空 POSIX 路径。

    Returns:
        规范化后的相对路径；解析后的路径归属另行校验。

    Raises:
        ToolError: 路径为绝对路径、为空，或包含目录越界及 NUL 字节。
    """
    value = PurePosixPath(path)
    if not path or value.is_absolute() or ".." in value.parts or "\x00" in path:
        raise ToolError(code="invalid_path", message="Use a workspace-relative path")
    return str(value)


# 此辅助脚本在已绑定的执行环境内运行，不在 Runtime 进程中执行。
# 在环境内再次解析路径以拒绝越界符号链接，容器是 shell 的执行边界。
_FILE_HELPER = r"""
import base64, codecs, json, pathlib, sys
request = json.loads(base64.b64decode(sys.argv[1]))
root = pathlib.Path.cwd().resolve()
path = (root / request['path']).resolve()
if not path.is_relative_to(root):
    raise ValueError('Path escapes workspace')
op = request['operation']
if op == 'read':
    with path.open('rb') as stream:
        data = stream.read(64001)
    text = codecs.getincrementaldecoder('utf-8')().decode(
        data[:64000], final=len(data) <= 64000)
    print(json.dumps({'text': text[:8000],
                      'truncated': len(data) > 64000 or len(text) > 8000},
                     ensure_ascii=False))
elif op == 'write':
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(request['content'], encoding='utf-8')
    print(json.dumps({'written': request['path']}))
elif op == 'list':
    rows = []
    for child in path.iterdir():
        rows.append(child.name + ('/' if child.is_dir() else ''))
        if len(rows) == 1000:
            break
    print(json.dumps({'entries': sorted(rows), 'limit_reached': len(rows) == 1000}))
elif op == 'replace':
    if path.stat().st_size > 1000000:
        raise ValueError('File too large for exact replacement')
    text = path.read_text(encoding='utf-8')
    old = request['old']
    if not old or text.count(old) != 1:
        raise ValueError('old_text must match exactly once')
    path.write_text(text.replace(old, request['new'], 1), encoding='utf-8')
    print(json.dumps({'updated': request['path']}))
"""


def _validate_command(command: str, timeout_seconds: float) -> None:
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
        raise ToolError(code="invalid_timeout", message="Timeout must be in (0,300]")
    if not command or len(command.encode()) > 64000:
        raise ToolError(
            code="invalid_command", message="Command must be 1..64000 bytes"
        )


def _file_output(result: CommandResult) -> ToolOutput:
    if result.exit_code != 0:
        raise ToolError(
            code="file_operation_failed",
            message=result.stderr or "File operation failed",
        )
    if result.truncated:
        return ToolOutput(
            content=json.dumps({"output": result.stdout, "truncated": True})
        )
    return ToolOutput(content=result.stdout)


def execution_tools(
    environment: ExecutionEnvironment, *, workspace: str = "/workspace"
) -> tuple[ToolDefinition, ...]:
    """构建显式启用的工具包，文件路径和工作目录限定在指定工作区内。

    Args:
        environment: 调用方持有且具有有限执行超时的命令接口。
        workspace: 已绑定执行环境中的绝对工作区路径。

    Returns:
        在同一工作区串行执行的固定命令与文件工具。

    Raises:
        ValueError: 工作区路径不是绝对路径。
    """
    if not PurePosixPath(workspace).is_absolute():
        raise ValueError("workspace must be an absolute environment path")

    async def run_command(
        command: str, cwd: str = ".", timeout_seconds: float = 60.0
    ) -> ToolOutput:
        """在当前任务环境中执行有超时约束的 shell 命令。

        Args:
            command: 非空 shell 命令，长度不超过 64000 个 UTF-8 字节。
            cwd: 相对于工作区的执行目录。
            timeout_seconds: 有限命令超时秒数，取值范围为 (0, 300]。

        Returns:
            序列化后的命令输出、退出码及截断和超时标记。

        Raises:
            ToolError: 命令大小、超时或工作目录无效。
        """
        _validate_command(command, timeout_seconds)
        directory = str(PurePosixPath(workspace) / relative_path(cwd))
        result = await environment.run(command, directory, timeout_seconds)
        return ToolOutput(content=json.dumps(asdict(result), ensure_ascii=False))

    async def file_operation(operation: str, path: str, **values: str) -> ToolOutput:
        """编码有大小限制的文件请求，供执行环境内处理。"""
        request = {"operation": operation, "path": relative_path(path), **values}
        if any(len(v.encode()) > 64000 for v in values.values()):
            raise ToolError(
                code="file_too_large", message="Maximum argument size is 64000 bytes"
            )
        encoded = base64.b64encode(
            json.dumps(request, ensure_ascii=False).encode()
        ).decode()
        if len(encoded) > 100000:
            raise ToolError(
                code="file_too_large", message="Encoded request exceeds 100000 bytes"
            )
        command = f"python3 -c {shlex.quote(_FILE_HELPER)} {shlex.quote(encoded)}"
        result = await environment.run(command, workspace, 30)
        return _file_output(result)

    async def read_file(path: str) -> ToolOutput:
        """读取工作区相对路径下的 UTF-8 文本，并限制读取大小。"""
        return await file_operation("read", path)

    async def write_file(path: str, content: str) -> ToolOutput:
        """在任务工作区内创建或覆盖 UTF-8 文件。"""
        return await file_operation("write", path, content=content)

    async def list_files(path: str = ".") -> ToolOutput:
        """列出工作区目录下最多 1000 个直接子项。"""
        return await file_operation("list", path)

    async def replace_text(path: str, old_text: str, new_text: str) -> ToolOutput:
        """替换唯一且精确匹配的文本，匹配不明确时拒绝修改。"""
        return await file_operation("replace", path, old=old_text, new=new_text)

    return tuple(
        build_function_tool(
            function, source=ToolSource.CUSTOMIZED, is_concurrency_safe=False
        )
        for function in (run_command, read_file, write_file, list_files, replace_text)
    )
