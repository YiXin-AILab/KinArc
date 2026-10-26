"""可选工具仅使用注入的执行环境。"""

import asyncio
import json
from pathlib import Path

import pytest
from agentsystem.common.errors import ToolError
from application.evoagentbench_knowledge_agent.plugins.execution import (
    CommandResult,
    execution_tools,
)


class TestWorkspace:
    """仅在 pytest 临时目录中执行可信的文件辅助代码。"""

    __test__ = False

    async def run(
        self, command: str, cwd: str, timeout_seconds: float
    ) -> CommandResult:
        async with asyncio.timeout(timeout_seconds):
            child = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, error = await child.communicate()
        return CommandResult(child.returncode or 0, output.decode(), error.decode())


@pytest.mark.asyncio
async def test_file_roundtrip_exact_edit_and_symlinks(tmp_path: Path) -> None:
    tools = {
        t.name: t for t in execution_tools(TestWorkspace(), workspace=str(tmp_path))
    }
    await tools["write_file"].handler.invoke(
        ctx=None, arguments={"path": "nested/file.txt", "content": "hello world"}
    )
    await tools["replace_text"].handler.invoke(
        ctx=None,
        arguments={"path": "nested/file.txt", "old_text": "world", "new_text": "agent"},
    )
    result = await tools["read_file"].handler.invoke(
        ctx=None, arguments={"path": "nested/file.txt"}
    )
    assert json.loads(result.content)["text"] == "hello agent"
    with pytest.raises(ToolError):
        await tools["replace_text"].handler.invoke(
            ctx=None,
            arguments={
                "path": "nested/file.txt",
                "old_text": "missing",
                "new_text": "x",
            },
        )
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ToolError):
        await tools["write_file"].handler.invoke(
            ctx=None, arguments={"path": "escape/forbidden.txt", "content": "x"}
        )
    assert not (tmp_path.parent / "forbidden.txt").exists()
    (tmp_path / "unicode.txt").write_text("中" * 22000)
    result = await tools["read_file"].handler.invoke(
        ctx=None, arguments={"path": "unicode.txt"}
    )
    preview = json.loads(result.content)
    assert preview["text"] == "中" * 8000
    assert preview["truncated"] is True
    (tmp_path / "emoji.txt").write_text("😀" * 8000)
    result = await tools["read_file"].handler.invoke(
        ctx=None, arguments={"path": "emoji.txt"}
    )
    assert len(result.content.encode()) < 64000
    assert json.loads(result.content)["text"] == "😀" * 8000


class RecordingEnvironment:
    """记录命令参数，不实际执行宿主命令。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    async def run(
        self, command: str, cwd: str, timeout_seconds: float
    ) -> CommandResult:
        self.calls.append((command, cwd, timeout_seconds))
        return CommandResult(exit_code=0, stdout="ok", stderr="")


@pytest.mark.asyncio
async def test_schema_and_command_are_explicitly_injected() -> None:
    env = RecordingEnvironment()
    tools = {t.name: t for t in execution_tools(env)}
    tool = tools["run_command"]
    assert not tool.is_concurrency_safe
    assert tool.invocation_model is not None
    arguments = tool.invocation_model.model_validate({"command": "python -V"})
    result = await tool.handler.invoke(ctx=None, arguments=arguments.model_dump())
    assert json.loads(result.content)["exit_code"] == 0
    assert env.calls == [("python -V", "/workspace", 60.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../secret"])
async def test_files_reject_paths_outside_workspace(path: str) -> None:
    env = RecordingEnvironment()
    tool = next(t for t in execution_tools(env) if t.name == "read_file")
    with pytest.raises(ToolError):
        await tool.handler.invoke(ctx=None, arguments={"path": path})
    assert not env.calls


@pytest.mark.asyncio
async def test_invalid_timeout_never_reaches_backend() -> None:
    env = RecordingEnvironment()
    tool = next(t for t in execution_tools(env) if t.name == "run_command")
    with pytest.raises(ToolError):
        await tool.handler.invoke(
            ctx=None, arguments={"command": "sleep 2", "timeout_seconds": 99999}
        )
    assert not env.calls
