"""基于文件的学习状态，以及各试验独立的临时记忆。"""

import asyncio
from pathlib import Path

from agentsystem.common.errors import ToolError
from agentsystem.common.models import StrictModel
from agentsystem.tool.builder import build_function_tool
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource
from pydantic import Field


class ToolDescriptions(StrictModel):
    """允许工具描述进化，工具能力保持固定。"""

    read_description: str = Field(min_length=1, max_length=8000)
    write_description: str = Field(min_length=1, max_length=8000)


def safe_path(root: Path, relative: str, *, write: bool = False) -> Path:
    """同时校验原始路径和解析路径的归属，并处理符号链接。"""
    path = Path(relative)
    allowed = {"memory"} if write else {"skills", "memory"}
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ToolError(code="invalid_path", message="Use a relative state path")
    if path.parts[0] not in allowed or len(path.parts) < 2:
        raise ToolError(code="invalid_path", message="Path must be under learned state")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ToolError(code="invalid_path", message="State path escapes its root")
    return resolved


def state_tools(target: Path, scratch: Path) -> tuple[ToolDefinition, ...]:
    """从候选资产加载工具描述，不允许工具访问评分数据。"""
    descriptions = ToolDescriptions.model_validate_json(
        (target / "tools.json").read_text()
    )

    async def read(path: str) -> ToolOutput:
        frozen = safe_path(target, path)
        transient = safe_path(scratch, path)
        selected = transient if transient.is_file() else frozen
        try:
            if selected.stat().st_size > 64000:
                raise ToolError(code="file_too_large", message="Maximum 64000 bytes")
            content = await asyncio.to_thread(selected.read_text, encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ToolError(
                code="file_unavailable", message="State file unavailable"
            ) from error
        return ToolOutput(content=content)

    async def write(path: str, content: str) -> ToolOutput:
        destination = safe_path(scratch, path, write=True)
        if len(content.encode()) > 64000:
            raise ToolError(code="file_too_large", message="Maximum 64000 bytes")
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_text, content, encoding="utf-8")
        return ToolOutput(content="saved to this trial only")

    return (
        build_function_tool(
            read,
            name="eab_read",
            source=ToolSource.CUSTOMIZED,
            description=descriptions.read_description,
        ),
        build_function_tool(
            write,
            name="eab_write_memory",
            source=ToolSource.CUSTOMIZED,
            description=descriptions.write_description,
        ),
    )
