"""Evolution 文件与证据工具的专属 Plugin 注册入口。"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from agentsystem.common.errors import ToolError
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.tool.models import ToolOutput
from pydantic import Field

from evolution.agent.plugins.evidence_tools import EvidenceTools
from evolution.agent.plugins.file_tools import WorkspaceTools

LOGGER = logging.getLogger(__name__)


class ToolThreads:
    """保留取消后仍在运行的文件线程，供请求结束前等待。"""

    def __init__(self) -> None:
        """初始化后台工具线程任务的完成凭证集合。"""
        self._tasks: set[asyncio.Task[ToolOutput]] = set()

    async def run[**P](
        self,
        function: Callable[P, ToolOutput],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> ToolOutput:
        """隔离等待方取消，确保线程完成前不会丢失停止证明。"""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._tasks.discard(task)

    async def wait(self) -> None:
        """等待遗留线程；外层负责预算，取消等待不能取消线程的完成凭证。"""
        tasks = tuple(self._tasks)
        if not tasks:
            return
        results = await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        self._tasks.difference_update(tasks)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, ToolError):
                log_unexpected_exception(
                    LOGGER,
                    "improver_tool_thread_failed",
                    result,
                    operation="evolution.tool_cleanup",
                )


def create_evolution_tool_plugin(
    files: WorkspaceTools,
    evidence: EvidenceTools,
    threads: ToolThreads | None = None,
) -> PluginSource:
    """构造绑定请求级工具服务的内置 PluginSource。"""
    threads = threads or ToolThreads()
    workspace_handlers = _WorkspaceHandlers(files, threads)
    evidence_handlers = _EvidenceHandlers(evidence, threads)

    def register(context: PluginSetupContext) -> None:
        """向当前插件上下文注册同一请求作用域的文件与证据工具。"""
        _register_workspace_tools(context, workspace_handlers)
        _register_evidence_tools(context, evidence_handlers)

    return PluginSource(
        plugin_id="builtin.evolution.tools",
        origin=PluginOrigin.BUILTIN,
        path=Path(__file__),
        register=register,
    )


class _EvidenceHandlers:
    """在线程中生成 Case 证据视图，保持 Runtime 事件循环可用。"""

    def __init__(self, evidence: EvidenceTools, threads: ToolThreads) -> None:
        """绑定只读证据服务及其线程生命周期管理器。"""
        self._evidence = evidence
        self._threads = threads

    async def read_case_evidence(
        self, case_id: Annotated[str, Field(min_length=1, strict=True)]
    ) -> ToolOutput:
        """在线程中读取 Case 评分及轨迹指引。"""
        return await self._threads.run(self._evidence.read_case_evidence, case_id)

    async def read_case_tracelog(
        self,
        case_id: Annotated[str, Field(min_length=1, strict=True)],
        offset: Annotated[int, Field(strict=True)] = 0,
        limit: Annotated[int, Field(strict=True)] | None = 500,
        truncate: Annotated[bool, Field(strict=True)] = True,
    ) -> ToolOutput:
        """在线程中按行读取 Case 轨迹。"""
        return await self._threads.run(
            self._evidence.read_case_tracelog, case_id, offset, limit, truncate
        )


class _WorkspaceHandlers:
    """在线程中执行同步 Workspace 文件操作。"""

    def __init__(self, files: WorkspaceTools, threads: ToolThreads) -> None:
        """绑定工作区服务，并创建跨文件操作的串行停止屏障。"""
        self._files = files
        self._threads = threads
        self._lock = asyncio.Lock()

    async def _run[**P](
        self,
        function: Callable[P, ToolOutput],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> ToolOutput:
        # 工具超时后线程可能仍在写入；停止屏障必须先于下一次文件操作。
        """串行执行文件操作，并先等待上一次超时遗留的线程结束。"""
        async with self._lock:
            await self._threads.wait()
            return await self._threads.run(function, *args, **kwargs)

    async def list_path(self, path: str = ".", max_depth: int = 1) -> ToolOutput:
        """在线程中列出 Workspace 路径。"""
        return await self._run(self._files.list_path, path, max_depth)

    async def read_file(
        self,
        path: str,
        offset: int = 0,
        limit: int = 200,
    ) -> ToolOutput:
        """在线程中读取 Workspace 文件。"""
        return await self._run(self._files.read_file, path, offset, limit)

    async def search_path(
        self,
        path: str,
        query: str,
        case_sensitive: bool = False,
        limit: int = 20,
    ) -> ToolOutput:
        """在线程中搜索 Workspace 文本。"""
        return await self._run(
            self._files.search_path,
            path,
            query,
            case_sensitive,
            limit,
        )

    async def write_file(
        self,
        action: Literal["create", "overwrite", "replace", "append", "prepend"],
        path: str,
        new_content: str,
        old_content: str | None = None,
        replace_all: bool = True,
    ) -> ToolOutput:
        """在线程中写入 Workspace 文件。"""
        return await self._run(
            self._files.write_file,
            action,
            path,
            new_content,
            old_content,
            replace_all,
        )

    async def make_directory(
        self,
        path: str,
        recursive: bool = False,
    ) -> ToolOutput:
        """在线程中创建 Workspace 目录。"""
        return await self._run(self._files.make_directory, path, recursive)

    async def move_path(self, source: str, destination: str) -> ToolOutput:
        """在线程中移动 Workspace 路径。"""
        return await self._run(self._files.move_path, source, destination)

    async def delete_path(self, path: str, recursive: bool = False) -> ToolOutput:
        """在线程中删除 Workspace 路径。"""
        return await self._run(self._files.delete_path, path, recursive)


def _register_workspace_tools(
    context: PluginSetupContext,
    files: _WorkspaceHandlers,
) -> None:
    """注册文件操作的公开 schema，并关闭同一工作区的并发执行。"""
    context.register_tool(
        files.list_path,
        name="list_path",
        description=(
            "列出 Staging Workspace 中指定目录的内容。max_depth=1 只列直接子项，"
            "更大的值继续展开子目录。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "相对于 Workspace Root 的可见目录路径；. 表示根目录。"
                    ),
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "递归列出的最大目录深度；1 只列出直接子项。",
                    "minimum": 1,
                    "default": 1,
                },
            },
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
        can_suspend=False,
    )
    context.register_tool(
        files.read_file,
        name="read_file",
        description=(
            "按行分页读取 UTF-8 文本文件。offset 表示跳过的行数，从 0 开始；"
            "limit 表示最多返回的行数，默认 200 行。返回路径标题、行号 TAB 正文"
            "和 next_offset/eof 尾注。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace 相对路径或任务指定的精确 Changelog 绝对路径。"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "从 0 开始跳过的行数。",
                    "minimum": 0,
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回的行数。",
                    "minimum": 1,
                    "default": 200,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
        can_suspend=False,
    )
    context.register_tool(
        files.search_path,
        name="search_path",
        description=(
            "在文本文件或目录中搜索字面文本，返回文件路径、从 1 开始的行号和"
            "所在行内容；目录会递归搜索。返回按文件路径标题分组的行号 TAB 正文，"
            "末尾给出匹配数及达到 limit 的提示。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace 相对路径或任务指定的精确 Changelog 绝对路径。"
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "要查找的非空字面文本，不是正则表达式。",
                    "minLength": 1,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "是否区分查询文本的大小写。",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回的匹配行数。",
                    "minimum": 1,
                    "default": 20,
                },
            },
            "required": ["path", "query"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
        can_suspend=False,
    )
    context.register_tool(
        files.write_file,
        name="write_file",
        description=(
            "创建、覆盖、追加或精确替换文本。action 支持 create、overwrite、"
            "replace、append、prepend；"
            "new_content 用于创建、覆盖、追加和替换；replace_all 默认替换全部匹配。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "create 创建新文件，overwrite 覆盖已有文件，replace "
                        "精确替换已有文本，append 追加文本，prepend 前置文本。"
                    ),
                    "enum": ["create", "overwrite", "replace", "append", "prepend"],
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace 相对路径或任务指定的精确 Changelog 绝对路径。"
                    ),
                },
                "old_content": {
                    "type": ["string", "null"],
                    "description": "replace 必填的非空原文；其他 action 忽略。",
                    "default": None,
                },
                "new_content": {
                    "type": "string",
                    "description": (
                        "创建、覆盖后的完整内容，或追加、替换的新文本；最多 256 KiB。"
                    ),
                },
                "replace_all": {
                    "type": "boolean",
                    "description": ("replace 时是否替换全部匹配；false 只替换第一个。"),
                    "default": True,
                },
            },
            "required": ["action", "path", "new_content"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
        can_suspend=False,
    )
    context.register_tool(
        files.make_directory,
        name="make_directory",
        description=(
            "在 Staging Workspace 的非保护路径中创建目录。"
            "recursive=true 时递归创建缺失的父目录。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于 Workspace Root 的可写目录路径。",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "是否同时创建缺失的父目录。",
                    "default": False,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
        can_suspend=False,
    )
    context.register_tool(
        files.move_path,
        name="move_path",
        description=(
            "移动或重命名 Staging Workspace 非保护路径中的文件或目录，不覆盖已有目标。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": ("相对于 Workspace Root 的可见非保护源路径。"),
                },
                "destination": {
                    "type": "string",
                    "description": (
                        "相对于 Workspace Root 的可写目标路径；不得已存在。"
                    ),
                },
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
        can_suspend=False,
    )
    context.register_tool(
        files.delete_path,
        name="delete_path",
        description=(
            "删除 Staging Workspace 非保护路径中的文件或目录。"
            "recursive=true 时递归删除非空目录。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": ("相对于 Workspace Root 的可见非保护路径。"),
                },
                "recursive": {
                    "type": "boolean",
                    "description": "删除非空目录时必须为 true。",
                    "default": False,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
        can_suspend=False,
    )


def _register_evidence_tools(
    context: PluginSetupContext,
    handlers: _EvidenceHandlers,
) -> None:
    """注册只读 Train 证据工具及其分页参数。"""
    context.register_tool(
        handlers.read_case_evidence,
        name="read_case_evidence",
        description=(
            "按 case_id 读取当前 Train Evaluation 中一个 Case 的评分证据，包括 result、"
            "expected、当前得分、上次得分、feedback 和执行元数据。"
            "tracelog 只返回行数和读取指引，不返回轨迹正文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "string",
                    "description": "user 提示词中参考评测列出的 Case ID。",
                    "minLength": 1,
                }
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
        is_concurrency_safe=True,
        can_suspend=False,
    )
    context.register_tool(
        handlers.read_case_tracelog,
        name="read_case_tracelog",
        description="读取指定 Case 的执行轨迹，用于分析 Agent 的执行过程和失败原因。",
        parameters={
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "string",
                    "description": "user 提示词中参考评测列出的 Case ID。",
                    "minLength": 1,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "从offset后开始读取",
                },
                "limit": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "default": 500,
                    "description": "返回的行数",
                },
                "truncate": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "仅限制 read_case_tracelog 的展示，每行最多 2,000 字符，"
                        "展开完整行用 false。"
                    ),
                },
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
        is_concurrency_safe=True,
        can_suspend=False,
    )


__all__ = ["create_evolution_tool_plugin"]
