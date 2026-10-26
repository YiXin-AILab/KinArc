"""skill_view Builtin Tool 分页和稳定错误协议测试。"""

import asyncio
import os
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
from agentsystem.common.errors import ToolError
from agentsystem.skill import index as skill_reader
from agentsystem.tool.models import ToolSource
from cachetools import TTLCache  # type: ignore[import-untyped]
from tests.unit.skills.skill_view_helpers import (
    execute_tool,
    invoke_tool,
    skill_view_tools,
)


async def test_reads_input_path_without_skill_index(tmp_path: Path) -> None:
    """未注入 SkillIndex 时也能按路径读取，并提供分页 schema。"""
    target = tmp_path / "standalone.md"
    target.write_text("独立读取", encoding="utf-8")
    tool = skill_view_tools((tmp_path,))[0]
    page = await invoke_tool(
        tool, {"name": "demo-skill", "file_path": str(target), "limit": 2}
    )
    assert page["content"] == "独立"
    assert page["next_offset"] == 2


def _files(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: demo\n"
        'metadata:\n  version: "1.0"\n---\nabcdef',
        encoding="utf-8",
    )
    (root / "detail.md").write_text("甲乙丙丁", encoding="utf-8")


async def test_skill_view_pages_unicode(
    tmp_path: Path,
) -> None:
    """分页 offset/limit 按 Unicode 字符计算。"""
    _files(tmp_path)
    definition = skill_view_tools((tmp_path,))[0]

    main = await invoke_tool(definition, {"name": "demo-skill", "limit": 8_000})
    page = await invoke_tool(
        definition,
        {
            "name": "demo-skill",
            "file_path": "detail.md",
            "offset": 1,
            "limit": 2,
        },
    )

    assert main["success"] is True
    assert page["content"] == "乙丙"
    assert page["truncated"] is True
    assert page["next_offset"] == 3


async def test_reads_new_file_without_index(tmp_path: Path) -> None:
    """清单只是发现入口，新文件可以直接读取。"""
    _files(tmp_path)
    definition = skill_view_tools((tmp_path,))[0]
    (tmp_path / "demo-skill" / "new.md").write_text("新内容", encoding="utf-8")
    page = await invoke_tool(definition, {"name": "demo-skill", "file_path": "new.md"})
    assert page["content"] == "新内容"
    assert page["next_offset"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("offset", True),
        ("offset", -1),
        ("offset", "1"),
        ("offset", 1.0),
        ("limit", False),
        ("limit", True),
        ("limit", 0),
        ("limit", 8_001),
        ("limit", "2"),
        ("limit", 2.0),
        ("file_path", 42),
        ("name", 42),
        ("unexpected", 1),
    ),
)
async def test_rejects_invalid_argument_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """真实执行入口拒绝非法参数，不将布尔值、字符串或浮点数转为整数。"""
    _files(tmp_path)
    tool = skill_view_tools((tmp_path,))[0]
    block = await execute_tool(tool, {"name": "demo-skill", field: value})
    assert block.is_error
    assert block.error_code == "invalid_tool_arguments"


async def test_skill_view_returns_stable_errors(tmp_path: Path) -> None:
    """缺失文件和非法分页参数抛出稳定 ToolError。"""
    tool = skill_view_tools((tmp_path,))[0]
    target = str(tmp_path / "missing.md")
    with pytest.raises(ToolError) as missing:
        await invoke_tool(tool, {"name": "demo-skill", "file_path": target})
    with pytest.raises(ToolError) as invalid:
        await invoke_tool(
            tool, {"name": "demo-skill", "file_path": target, "limit": 8_001}
        )
    assert missing.value.code == "skill_file_not_found"
    assert str(missing.value) == f"找不到文件：{target}"
    assert invalid.value.code == "invalid_tool_arguments"


def test_skill_view_registers_without_index() -> None:
    """Builtin Tool 注册不需要 SkillIndex 服务。"""
    tools = skill_view_tools()
    assert len(tools) == 1
    assert tools[0].name == "skill_view"
    assert tools[0].source is ToolSource.BUILTIN
    properties = cast(dict[str, Any], tools[0].parameters["properties"])
    assert properties["name"]["type"] == "string"
    assert "enum" not in properties["name"]
    assert tools[0].parameters["required"] == ["name"]
    assert properties["offset"]["minimum"] == 0
    assert properties["limit"]["type"] == "integer"
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 8_000
    assert properties["limit"]["default"] == 8_000


async def test_ttl_cache_refreshes_and_does_not_hide_deleted_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL 和 mtime 控制刷新，缓存不能掩盖文件删除。"""
    now = [0.0]
    monkeypatch.setattr(
        skill_reader,
        "_CACHE",
        TTLCache(maxsize=1_024, ttl=60, timer=lambda: now[0]),
    )
    _files(tmp_path)
    definition = skill_view_tools((tmp_path,))[0]
    target = tmp_path / "demo-skill" / "detail.md"
    original = target.stat()
    args: dict[str, object] = {"name": "demo-skill", "file_path": "detail.md"}
    assert (await invoke_tool(definition, args))["content"] == "甲乙丙丁"
    target.write_text("新内容", encoding="utf-8")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert (await invoke_tool(definition, args))["content"] == "甲乙丙丁"
    now[0] = 60.0
    assert (await invoke_tool(definition, args))["content"] == "新内容"
    target.write_text("再次修改", encoding="utf-8")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000))
    assert (await invoke_tool(definition, args))["content"] == "再次修改"
    target.unlink()
    with pytest.raises(ToolError) as error:
        await invoke_tool(definition, args)
    assert error.value.code == "skill_file_not_found"


async def test_pages_reconstruct_long_main_file(tmp_path: Path) -> None:
    """长主文件逐页读取不丢字符。"""
    _files(tmp_path)
    main = tmp_path / "demo-skill" / "SKILL.md"
    text = main.read_text(encoding="utf-8") + "中文🙂" * 6_000
    main.write_text(text, encoding="utf-8")
    definition = skill_view_tools((tmp_path,))[0]
    offset = 0
    parts = []
    while True:
        page = await invoke_tool(definition, {"name": "demo-skill", "offset": offset})
        parts.append(page["content"])
        if not page["truncated"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(parts) == text


@pytest.mark.parametrize("content", ("", "甲乙丙丁"))
async def test_page_at_end_is_empty_and_past_end_errors(
    tmp_path: Path,
    content: str,
) -> None:
    """末尾 offset 可读空页，超过长度返回稳定错误。"""
    _files(tmp_path)
    definition = skill_view_tools((tmp_path,))[0]
    (tmp_path / "demo-skill" / "detail.md").write_text(content, encoding="utf-8")
    args = {"name": "demo-skill", "file_path": "detail.md", "offset": len(content)}
    page = await invoke_tool(definition, args)
    assert page["content"] == ""
    assert page["next_offset"] is None
    assert page["truncated"] is False
    args["offset"] = len(content) + 1
    with pytest.raises(ToolError) as error:
        await invoke_tool(definition, args)
    assert error.value.code == "skill_offset_out_of_range"


@pytest.mark.parametrize("content", (b"\xff", b"text\x00more"))
async def test_rejects_invalid_text_at_read_time(
    tmp_path: Path, content: bytes
) -> None:
    """二进制内容可以登记但不能作为文本页读取。"""
    _files(tmp_path)
    (tmp_path / "demo-skill" / "detail.md").write_bytes(content)
    definition = skill_view_tools((tmp_path,))[0]
    with pytest.raises(ToolError) as error:
        await invoke_tool(definition, {"name": "demo-skill", "file_path": "detail.md"})
    assert error.value.code == "skill_file_not_readable"


async def test_parallel_reads_share_cache(tmp_path: Path) -> None:
    """并发页读取可以共享 TTL 缓存。"""
    _files(tmp_path)
    definition = skill_view_tools((tmp_path,))[0]
    pages = await asyncio.gather(
        *(
            invoke_tool(definition, {"name": "demo-skill", "file_path": "detail.md"})
            for _ in range(32)
        )
    )
    assert all(page["content"] == "甲乙丙丁" for page in pages)


@pytest.mark.parametrize("kind", ("directory", "permission"))
async def test_read_failure_is_not_reported_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """目录和权限失败不是找不到文件。"""
    _files(tmp_path)
    definition = skill_view_tools((tmp_path,))[0]
    resource = "" if kind == "directory" else "detail.md"
    if kind == "permission":

        def denied(*args: object, **kwargs: object) -> None:
            raise PermissionError("test denied")

        monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ToolError) as error:
        await invoke_tool(definition, {"name": "demo-skill", "file_path": resource})
    assert error.value.code == "skill_file_not_readable"


async def test_cancelled_read_stops_before_next_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消后不继续扫描后续块，并释放正在读取的文件。"""
    _files(tmp_path)
    definition = skill_view_tools((tmp_path,))[0]
    entered, release, finished = Event(), Event(), Event()
    original_read = os.read
    original_text = skill_reader._read_text
    read_calls: list[int] = []

    def read(descriptor: int, count: int) -> bytes:
        read_calls.append(descriptor)
        if len(read_calls) == 1:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release reader")
        return original_read(descriptor, count)

    def text(*args: Any, **kwargs: Any) -> tuple[str, int]:
        try:
            return original_text(*args, **kwargs)
        finally:
            finished.set()

    from agentsystem.skill.plugins import skill_view_tool

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(skill_view_tool, "_read_text", text)
    task = asyncio.create_task(
        invoke_tool(
            definition,
            {"name": "demo-skill", "file_path": "detail.md", "offset": 100},
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert len(read_calls) == 1
    with pytest.raises(OSError):
        os.fstat(read_calls[0])


async def test_reads_new_skill_after_tool_registration(tmp_path: Path) -> None:
    """工具不依赖启动期 SkillIndex，新建 Skill 可按名称读取。"""
    tool = skill_view_tools((tmp_path,))[0]
    _files(tmp_path)
    page = await invoke_tool(tool, {"name": "demo-skill"})
    assert "abcdef" in page["content"]


async def test_application_directory_wins_without_resource_fallback(
    tmp_path: Path,
) -> None:
    """应用目录优先，应用缺失的资源不能回退到全局同名 Skill。"""
    application, global_root = tmp_path / "app", tmp_path / "global"
    application.mkdir()
    global_root.mkdir()
    _files(global_root)
    tool = skill_view_tools((application, global_root))[0]
    assert (await invoke_tool(tool, {"name": "demo-skill", "file_path": "detail.md"}))[
        "content"
    ] == "甲乙丙丁"
    _files(application)
    (application / "demo-skill" / "detail.md").unlink()
    with pytest.raises(ToolError) as error:
        await invoke_tool(tool, {"name": "demo-skill", "file_path": "detail.md"})
    assert error.value.code == "skill_file_not_found"


async def test_absolute_path_does_not_probe_skill_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绝对资源路径直接读取，不受无关 Skill 目录权限影响。"""
    target = tmp_path / "standalone.md"
    target.write_text("直接读取", encoding="utf-8")
    tool = skill_view_tools((tmp_path / "unavailable",))[0]

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("test denied")

    monkeypatch.setattr(Path, "lstat", denied)
    page = await invoke_tool(tool, {"name": "demo-skill", "file_path": str(target)})
    assert page["content"] == "直接读取"
