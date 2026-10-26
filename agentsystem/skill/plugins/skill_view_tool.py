"""注册并实现当前 Agent 的 skill_view Tool。"""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from threading import Event
from typing import Annotated

from pydantic import Field

from agentsystem.common.errors import ToolError
from agentsystem.common.models import JsonValue
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.skill.index import _read_text
from agentsystem.skill.models import SkillRoots
from agentsystem.tool.models import ToolOutput

DEFAULT_LIMIT = 8_000
MAX_LIMIT = 8_000


def _resolve_path(roots: tuple[Path, ...], name: str, logical: str) -> Path:
    path = Path(logical)
    if path.is_absolute():
        return path
    for root in roots:
        directory = root / name
        try:
            directory.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        return directory / path
    return (roots[0] if roots else Path()) / name / path


def _create_payload(
    roots: tuple[Path, ...],
    name: str,
    file_path: str | None,
    offset: int,
    limit: int,
    cancelled: Event,
) -> dict[str, JsonValue]:
    logical = "SKILL.md" if file_path is None else file_path
    try:
        path = _resolve_path(roots, name, logical)
        content, length = _read_text(path, offset, limit, cancelled)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ToolError(
            code="skill_file_not_found",
            message=f"找不到文件：{logical}",
        ) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise ToolError(
            code="skill_file_not_readable",
            message="Skill 文件无法读取",
        ) from error
    if offset > length:
        raise ToolError(
            code="skill_offset_out_of_range",
            message="offset 超出 Skill 文件文本长度",
        )
    truncated = length > offset + limit
    payload: dict[str, JsonValue] = {
        "success": True,
        "name": name,
        "file": logical,
        "content": content[:limit],
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "next_offset": offset + limit if truncated else None,
    }
    return payload


def _output(payload: Mapping[str, JsonValue]) -> ToolOutput:
    return ToolOutput(
        content=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def register(context: PluginSetupContext) -> None:
    """注册直接按输入路径分页读取文本的 builtin Tool。

    Args:
        context: 提供 SkillRoots 能力和工具注册入口的插件上下文。

    运行期读取不重新校验路径归属或索引；文件 I/O 在线程中执行并协作取消。
    """
    roots = context.require(SkillRoots).paths

    async def skill_view(
        name: str,
        file_path: str | None = None,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> ToolOutput:
        """分页读取 Skill 指令或 UTF-8 文本资源。

        Args:
            name: 用于应用优先目录查找的 Skill 名称。
            file_path: 相对 Skill 目录的路径或绝对路径；缺省读取 SKILL.md。
            offset: 按 Unicode 字符计数的起始偏移。
            limit: 单页最大字符数。

        Returns:
            带正文、截断标记和下一页偏移的 JSON 文本。

        Raises:
            ToolError: 文件不存在、不可读或 offset 超出文本长度。
        """
        cancelled = Event()
        try:
            return _output(
                await asyncio.to_thread(
                    _create_payload,
                    roots,
                    name,
                    file_path,
                    offset,
                    limit,
                    cancelled,
                )
            )
        finally:
            cancelled.set()

    context.register_tool(
        skill_view,
        name="skill_view",
        description="读取指定 Skill 或该 Skill 关联的文本文件。",
        rules="使用 Skill 时，先读取 SKILL.md，按需读取 linked_files 中的关联资源。",
    )
