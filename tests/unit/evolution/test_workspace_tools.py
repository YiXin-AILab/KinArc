"""Evolution Agent Workspace 文件工具测试。"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

import pytest
from agentsystem.common.errors import ToolError
from agentsystem.tool.models import ToolOutput
from evolution.agent.plugins.file_tools import WorkspaceTools
from evolution.diagnosis import build_diagnosis_brief
from evolution.improver import ImprovementRequest
from evolution.models import EvaluationResult


def _request(tmp_path: Path) -> ImprovementRequest:
    workspace = tmp_path / "workspace"
    (workspace / "prompt" / "nested").mkdir(parents=True)
    (workspace / "data").mkdir()
    (workspace / "prompt" / "system.md").write_text(
        "first\nSecond match\nthird match\n",
        encoding="utf-8",
    )
    (workspace / "prompt" / "nested" / "skill.md").write_text(
        "nested match\n",
        encoding="utf-8",
    )
    (workspace / "private.txt").write_text("private\n", encoding="utf-8")
    (workspace / "data" / "db.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "changelog.md").write_text("")
    return ImprovementRequest(
        objective="improve",
        base_candidate_id="candidate-1",
        changelog_source_candidate_id="candidate-1",
        evidence_candidate_id="candidate-1",
        output_candidate_id="candidate-output",
        evidence_parent_candidate_id=None,
        workspace_dir=workspace,
        read_only=("private.txt", "data/", "reserved/"),
        inaccessible=("**/.*", "**/__pycache__"),
        train_evaluation=EvaluationResult(total_score=0, cases=()),
        diagnosis_brief=build_diagnosis_brief(
            "candidate-1", EvaluationResult(total_score=0, cases=())
        ),
        changelog_path=tmp_path / "changelog.md",
    )


def _content(output: ToolOutput) -> dict[str, object]:
    return cast(dict[str, object], json.loads(output.content))


def _assert_code(error: pytest.ExceptionInfo[ToolError], code: str) -> None:
    assert error.value.code == code


def test_tools_require_a_bound_request() -> None:
    """缺少请求绑定时不得访问任何默认目录。"""
    tools = WorkspaceTools()

    with pytest.raises(ToolError) as captured:
        tools.list_path()
    _assert_code(captured, "request_not_bound")


@pytest.mark.parametrize("mode", [0o755, 0o640])
def test_edit_preserves_existing_file_mode(tmp_path: Path, mode: int) -> None:
    """编辑已有文件只改变正文，不丢失执行位或组读取权限。"""
    request = _request(tmp_path)
    path = request.workspace_dir / "prompt/system.md"
    path.chmod(mode)
    tools = WorkspaceTools()
    tools.bind(request)
    tools.write_file("replace", "prompt/system.md", "updated", "first")
    assert path.read_text().startswith("updated\n")
    assert path.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize(
    "old",
    ["", "## 迭代版本：C\nMARK_C\n", "x" * 300_000],
    ids=["empty", "group", "large"],
)
def test_prepend_changelog_preserves_existing_content(tmp_path: Path, old: str) -> None:
    request = _request(tmp_path)
    request.changelog_path.write_text(old)
    tools = WorkspaceTools()
    tools.bind(request)
    new = "## 迭代版本：D\nMARK_D\n\n"
    tools.write_file("prepend", str(request.changelog_path), new)
    assert request.changelog_path.read_text() == new + old
    if "MARK_C" in old:
        tools.write_file(
            "replace", str(request.changelog_path), "通过", "MARK_C", False
        )
        assert request.changelog_path.read_text() == new + old.replace("MARK_C", "通过")


@pytest.mark.parametrize(
    "path,content",
    [
        ("private.txt", "x"),
        ("prompt/system.md", "x" * 262_144),
        ("changelog", "x" * 262_145),
    ],
    ids=["read-only", "file-limit", "fragment-limit"],
)
def test_prepend_keeps_write_permissions_and_limits(
    tmp_path: Path,
    path: str,
    content: str,
) -> None:
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)
    with pytest.raises(ToolError):
        tools.write_file(
            "prepend",
            str(request.changelog_path) if path == "changelog" else path,
            content,
        )


def test_append_and_replace_large_changelog(tmp_path: Path) -> None:
    """大日志局部编辑保留历史，标记独立替换且支持按行检索。"""
    request = _request(tmp_path)
    log = tmp_path / "changelog.md"
    request = request.model_copy(update={"changelog_path": log})
    history = "history\n" * 40000
    log.write_text(history)
    tools = WorkspaceTools()
    tools.bind(request)
    first, second = "CHANGE_RESULT_candidate-1_one", "CHANGE_RESULT_candidate-1_two"
    tools.write_file("append", str(log), first + "\n" + second + "\n")
    assert "matches=1" in tools.search_path(str(log), first).content
    result = _content(
        tools.write_file("replace", str(log), "case-1: improved", first, False)
    )
    assert result["replacements"] == 1
    assert log.read_text() == history + "case-1: improved\n" + second + "\n"
    assert second in tools.read_file(str(log), offset=40001).content
    assert "matches=0" in tools.search_path(".", second).content


@pytest.mark.parametrize("operation", ["read", "search", "write", "prepend"])
@pytest.mark.parametrize(
    "node", ["other", "traversal", "alias", "symlink", "parent_link", "directory"]
)
def test_changelog_access_is_exact_and_rejects_links(
    tmp_path: Path,
    operation: str,
    node: str,
) -> None:
    """精确允许单个日志，其他路径及任一级符号链接不能借此越界。"""
    request = _request(tmp_path)
    log = request.changelog_path
    path = str(log)
    if node == "other":
        path = str(tmp_path / "other.md")
        Path(path).write_text("private")
    elif node == "traversal":
        path = str(log.parent) + "/workspace/../changelog.md"
    elif node == "alias":
        path = str(log.parent) + "/./changelog.md"
    elif node == "symlink":
        log.unlink()
        log.symlink_to(tmp_path / "missing")
    elif node == "parent_link":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        log = link / "changelog.md"
        path = str(log)
        request = request.model_copy(update={"changelog_path": log})
    else:
        log.unlink()
        log.mkdir()
    tools = WorkspaceTools()
    tools.bind(request)
    with pytest.raises(ToolError):
        if operation == "read":
            tools.read_file(path)
        elif operation == "search":
            tools.search_path(path, "private")
        elif operation == "prepend":
            tools.write_file("prepend", path, "changed")
        else:
            tools.write_file("append", path, "changed")


def test_changelog_cannot_be_moved_deleted_or_accessed_after_rebind(
    tmp_path: Path,
) -> None:
    """日志特例不开放目录变更，解绑或换轮撤销旧路径访问。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)
    path = str(request.changelog_path)
    operations: tuple[Callable[[], ToolOutput], ...] = (
        lambda: tools.move_path(path, "new.md"),
        lambda: tools.move_path("prompt/system.md", path),
        lambda: tools.delete_path(path),
        lambda: tools.make_directory(path),
        lambda: tools.list_path(path),
    )
    for operation in operations:
        with pytest.raises(ToolError):
            operation()
    tools.unbind()
    with pytest.raises(ToolError):
        tools.read_file(path)
    tools.bind(_request(tmp_path / "next"))
    with pytest.raises(ToolError):
        tools.read_file(path)
    assert request.changelog_path.read_text() == ""


@pytest.mark.parametrize("action", ["append", "replace", "prepend"])
def test_changelog_input_limit_and_failed_write_preserve_history(
    tmp_path: Path,
    action: Literal["append", "replace", "prepend"],
) -> None:
    """日志豁免最终大小，不豁免输入限制。"""
    request = _request(tmp_path)
    request.changelog_path.write_text("history")
    tools = WorkspaceTools()
    tools.bind(request)
    with pytest.raises(ToolError) as error:
        tools.write_file(action, str(request.changelog_path), "x" * 262145, "history")
    assert error.value.code == "content_too_large"
    assert request.changelog_path.read_text() == "history"


def test_append_workspace_and_limits(tmp_path: Path) -> None:
    """普通文件追加保留大小限制，失败不改变原内容。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)
    target = request.workspace_dir / "prompt/system.md"
    original = target.read_text()
    tools.write_file("append", "prompt/system.md", "last\n")
    assert target.read_text() == original + "last\n"
    with pytest.raises(ToolError) as error:
        tools.write_file("append", "prompt/system.md", "x" * 262144)
    assert error.value.code == "content_too_large"
    assert target.read_text() == original + "last\n"


def test_list_path_honors_max_depth_and_reports_file_size(tmp_path: Path) -> None:
    """错误的深度判断会漏列或越界展开 Workspace。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))

    shallow = _content(tools.list_path("prompt", max_depth=1))
    deep = _content(tools.list_path("prompt", max_depth=2))

    assert shallow == {
        "path": "prompt",
        "entries": [
            {"path": "prompt/nested", "type": "directory"},
            {"path": "prompt/system.md", "type": "file", "size_bytes": 31},
        ],
    }
    assert deep["entries"] == [
        {"path": "prompt/nested", "type": "directory"},
        {"path": "prompt/nested/skill.md", "type": "file", "size_bytes": 13},
        {"path": "prompt/system.md", "type": "file", "size_bytes": 31},
    ]
    assert "truncated" not in deep


def test_read_file_pages_by_lines(tmp_path: Path) -> None:
    """把 offset 当字符偏移会返回错误的证据上下文。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))

    assert tools.read_file("prompt/system.md", offset=0, limit=2).content == (
        "--- prompt/system.md\n1\tfirst\n2\tSecond match\n--- next_offset=2 eof=false"
    )
    assert tools.read_file("prompt/system.md", offset=2, limit=2).content == (
        "--- prompt/system.md\n3\tthird match\n--- next_offset=3 eof=true"
    )


def test_search_path_recurses_and_returns_full_numbered_lines(tmp_path: Path) -> None:
    """目录搜索必须稳定返回完整匹配行及 1-based 行号。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))

    assert tools.search_path("prompt", "match", limit=2).content == (
        "--- prompt/nested/skill.md\n1\tnested match\n"
        "--- prompt/system.md\n2\tSecond match\n--- matches=2 limit=2\n"
        "--- 已达到匹配数量上限，可能还有其他匹配。"
    )
    assert tools.search_path("prompt/system.md", "match").content == (
        "--- prompt/system.md\n2\tSecond match\n3\tthird match\n--- matches=2 limit=20"
    )


def test_search_path_respects_case_sensitive(tmp_path: Path) -> None:
    """case_sensitive=true 时不得命中大小写不同的文本。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))

    result = tools.search_path(
        "prompt/system.md",
        "second",
        case_sensitive=True,
    ).content

    assert result == "--- matches=0 limit=20"


def test_hidden_and_pycache_paths_are_absent_from_listing_and_search(
    tmp_path: Path,
) -> None:
    """默认隐藏路径不得进入 Agent 的目录视图或搜索结果。"""
    request = _request(tmp_path)
    (request.workspace_dir / ".env").write_text("hidden match\n", encoding="utf-8")
    hidden_dir = request.workspace_dir / "prompt" / ".internal"
    hidden_dir.mkdir()
    (hidden_dir / "notes.txt").write_text("hidden match\n", encoding="utf-8")
    cache_dir = request.workspace_dir / "prompt" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "module.pyc").write_text("hidden match\n", encoding="utf-8")
    tools = WorkspaceTools()
    tools.bind(request)

    listing = _content(tools.list_path(".", max_depth=4))
    search = tools.search_path(".", "hidden match").content

    listed_paths = {
        entry["path"] for entry in cast(list[dict[str, object]], listing["entries"])
    }
    assert ".env" not in listed_paths
    assert "prompt/.internal" not in listed_paths
    assert "prompt/__pycache__" not in listed_paths
    assert search == "--- matches=0 limit=20"


def test_read_and_search_preserve_whitespace_and_long_lines(tmp_path: Path) -> None:
    """文本视图不能对文件内容新增截断或去掉缩进。"""
    request = _request(tmp_path)
    path = request.workspace_dir / "sample.txt"
    original = "\n--- original\n\t  " + "x" * 30000 + " \n"
    path.write_text(original, encoding="utf-8")
    tools = WorkspaceTools()
    tools.bind(request)
    assert tools.read_file("sample.txt").content == (
        "--- sample.txt\n1\t\n2\t--- original\n3\t\t  "
        + "x" * 30000
        + " \n--- next_offset=3 eof=true"
    )
    assert tools.search_path("sample.txt", "xxx").content == (
        "--- sample.txt\n3\t\t  " + "x" * 30000 + " \n--- matches=1 limit=20"
    )
    assert tools.read_file("sample.txt", offset=10).content == (
        "--- sample.txt\n--- next_offset=10 eof=true"
    )
    assert path.read_text(encoding="utf-8") == original


def test_read_empty_file_returns_only_pagination(tmp_path: Path) -> None:
    """空文件不应产生伪造正文行。"""
    request = _request(tmp_path)
    (request.workspace_dir / "empty.txt").write_text("", encoding="utf-8")
    tools = WorkspaceTools()
    tools.bind(request)
    assert tools.read_file("empty.txt").content == (
        "--- empty.txt\n--- next_offset=0 eof=true"
    )


@pytest.mark.parametrize(
    "path",
    (".env", "prompt/.internal/notes.txt", "prompt/__pycache__/module.pyc"),
)
def test_hidden_and_pycache_paths_cannot_be_accessed(
    tmp_path: Path,
    path: str,
) -> None:
    """直接访问不可见路径必须与访问不存在的路径相同。"""
    request = _request(tmp_path)
    target = request.workspace_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret\n", encoding="utf-8")
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as read_error:
        tools.read_file(path)
    with pytest.raises(ToolError) as write_error:
        tools.write_file("overwrite", path, "changed\n")

    _assert_code(read_error, "path_not_found")
    _assert_code(write_error, "path_not_found")
    assert target.read_text(encoding="utf-8") == "secret\n"


def test_move_and_delete_reject_directory_containing_hidden_content(
    tmp_path: Path,
) -> None:
    """目录操作不得间接移动或删除不可见内容。"""
    request = _request(tmp_path)
    (request.workspace_dir / "prompt" / ".state").write_text(
        "hidden\n",
        encoding="utf-8",
    )
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as move_error:
        tools.move_path("prompt", "archive")
    with pytest.raises(ToolError) as delete_error:
        tools.delete_path("prompt", recursive=True)

    _assert_code(move_error, "path_not_found")
    _assert_code(delete_error, "path_not_found")
    assert (request.workspace_dir / "prompt" / ".state").is_file()


@pytest.mark.parametrize("path", ("", "/tmp/file", "../file", "a/./b", "a\\b"))
def test_tools_reject_invalid_paths(tmp_path: Path, path: str) -> None:
    """非法路径不得被 resolve 成一个看似安全的 Workspace 路径。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))

    with pytest.raises(ToolError) as captured:
        tools.read_file(path)

    _assert_code(captured, "invalid_path")


def test_tools_reject_symlink_components(tmp_path: Path) -> None:
    """中间目录符号链接不得绕过 Workspace 边界。"""
    request = _request(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (request.workspace_dir / "linked").symlink_to(outside, target_is_directory=True)
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.read_file("linked/secret.txt")

    _assert_code(captured, "symlink_forbidden")


def test_write_file_create_overwrite_and_replace(tmp_path: Path) -> None:
    """错误的 action 分支会覆盖已有文件或替换错误数量。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)

    created = _content(
        tools.write_file("create", "skills/new/SKILL.md", "alpha alpha\n")
    )
    replaced = _content(
        tools.write_file(
            "replace",
            "skills/new/SKILL.md",
            "beta",
            old_content="alpha",
            replace_all=False,
        )
    )
    overwritten = _content(
        tools.write_file("overwrite", "skills/new/SKILL.md", "final\n")
    )

    assert created["action"] == "create"
    assert replaced["replacements"] == 1
    assert overwritten["sha256"]
    assert (request.workspace_dir / "skills/new/SKILL.md").read_text() == "final\n"


def test_write_file_replaces_all_by_default(tmp_path: Path) -> None:
    """replace_all 默认值错误会只修复第一个重复片段。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)

    result = _content(
        tools.write_file(
            "replace",
            "prompt/system.md",
            "fixed",
            old_content="match",
        )
    )

    assert result["replacements"] == 2
    assert "match" not in (request.workspace_dir / "prompt/system.md").read_text()


@pytest.mark.parametrize("path", ("private.txt", "data/db.json", "reserved/new.txt"))
def test_write_file_rejects_read_only(tmp_path: Path, path: str) -> None:
    """精确文件、目录树和尚不存在的保护路径都必须 fail-close。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))
    action: Literal["create", "overwrite"] = (
        "overwrite" if path != "reserved/new.txt" else "create"
    )

    with pytest.raises(ToolError) as captured:
        tools.write_file(action, path, "changed")

    _assert_code(captured, "protected_path")


def test_write_file_failure_leaves_original_content(tmp_path: Path) -> None:
    """找不到 old_content 时不得产生部分写入。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.write_file(
            "replace",
            "prompt/system.md",
            "new",
            old_content="missing",
        )

    _assert_code(captured, "text_not_found")
    assert (
        (request.workspace_dir / "prompt/system.md").read_text().startswith("first\n")
    )


def test_write_file_checks_complete_replacement_size(tmp_path: Path) -> None:
    """小替换片段不得把最终文件膨胀到 256 KiB 上限之外。"""
    request = _request(tmp_path)
    target = request.workspace_dir / "large.txt"
    target.write_text("x" * 140_000, encoding="utf-8")
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.write_file(
            "replace",
            "large.txt",
            "yy",
            old_content="x",
        )

    _assert_code(captured, "content_too_large")
    assert target.stat().st_size == 140_000


def test_write_file_rejects_non_utf8_replacement_target(tmp_path: Path) -> None:
    """replace 只支持 UTF-8 文本，且不得泄漏底层解码异常。"""
    request = _request(tmp_path)
    target = request.workspace_dir / "binary.bin"
    original = b"old\xffcontent"
    target.write_bytes(original)
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.write_file(
            "replace",
            "binary.bin",
            "new",
            old_content="old",
        )

    _assert_code(captured, "unsupported_file_type")
    assert target.read_bytes() == original


def test_atomic_write_failure_preserves_original_and_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原子替换失败时必须保留原文件并清理同目录临时文件。"""
    request = _request(tmp_path)
    target = request.workspace_dir / "prompt/system.md"
    original = target.read_bytes()
    tools = WorkspaceTools()
    tools.bind(request)

    def fail_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        tools.write_file("overwrite", "prompt/system.md", "changed\n")

    assert target.read_bytes() == original
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_make_directory_honors_recursive_flag(tmp_path: Path) -> None:
    """recursive=false 不得隐式创建缺失父目录。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.make_directory("skills/example/assets", recursive=False)
    _assert_code(captured, "path_not_found")

    result = _content(tools.make_directory("skills/example/assets", recursive=True))
    assert result == {"path": "skills/example/assets"}
    assert (request.workspace_dir / "skills/example/assets").is_dir()


def test_tools_report_file_used_as_parent(tmp_path: Path) -> None:
    """文件被当作父目录时不得泄漏原始文件系统异常。"""
    request = _request(tmp_path)
    (request.workspace_dir / "blocker").write_text("file", encoding="utf-8")
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.make_directory("blocker/child", recursive=True)

    _assert_code(captured, "not_directory")


def test_move_path_creates_parent_without_overwriting(tmp_path: Path) -> None:
    """移动必须创建目标父目录且绝不覆盖已有目标。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)

    result = _content(tools.move_path("prompt/nested", "skills/example"))
    assert result == {"source": "prompt/nested", "destination": "skills/example"}
    assert (request.workspace_dir / "skills/example/skill.md").is_file()

    (request.workspace_dir / "occupied").write_text("occupied", encoding="utf-8")
    with pytest.raises(ToolError) as captured:
        tools.move_path("prompt/system.md", "occupied")
    _assert_code(captured, "destination_exists")


def test_move_and_delete_reject_ancestor_of_protected_path(tmp_path: Path) -> None:
    """目录操作不得通过移动或删除父目录间接影响 protected 后代。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))

    with pytest.raises(ToolError) as move_error:
        tools.move_path("data", "archive")
    with pytest.raises(ToolError) as delete_error:
        tools.delete_path("data", recursive=True)

    _assert_code(move_error, "protected_path")
    _assert_code(delete_error, "protected_path")


def test_delete_path_requires_recursive_for_non_empty_directory(tmp_path: Path) -> None:
    """非空目录只有显式 recursive=true 才能删除。"""
    request = _request(tmp_path)
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.delete_path("prompt")
    _assert_code(captured, "directory_not_empty")

    assert _content(tools.delete_path("prompt", recursive=True)) == {"path": "prompt"}
    assert not (request.workspace_dir / "prompt").exists()


def test_delete_path_rejects_workspace_root(tmp_path: Path) -> None:
    """Workspace 根目录永远不能被删除。"""
    tools = WorkspaceTools()
    tools.bind(_request(tmp_path))

    with pytest.raises(ToolError) as captured:
        tools.delete_path(".", recursive=True)

    _assert_code(captured, "invalid_path")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="平台不支持 FIFO")
def test_tools_reject_special_files(tmp_path: Path) -> None:
    """特殊文件不得进入读取或递归变更路径。"""
    request = _request(tmp_path)
    os.mkfifo(request.workspace_dir / "pipe")
    tools = WorkspaceTools()
    tools.bind(request)

    with pytest.raises(ToolError) as captured:
        tools.read_file("pipe")

    _assert_code(captured, "unsupported_file_type")


@pytest.mark.parametrize("operation", ("move", "delete"))
def test_read_only_glob_blocks_source_file(tmp_path: Path, operation: str) -> None:
    """Glob 保护必须覆盖移动和删除的源文件。"""
    request = _request(tmp_path).model_copy(update={"read_only": ("**/*.md",)})
    tools = WorkspaceTools()
    tools.bind(request)
    with pytest.raises(ToolError) as captured:
        if operation == "move":
            tools.move_path("prompt/system.md", "renamed.txt")
        else:
            tools.delete_path("prompt/system.md")
    assert captured.value.code == "protected_path"
    assert (request.workspace_dir / "prompt/system.md").is_file()


def test_inaccessible_rules_hide_plain_paths_and_all_operations(tmp_path: Path) -> None:
    """普通路径同样可隐藏，且所有文件入口都拒绝访问。"""
    request = _request(tmp_path).model_copy(update={"inaccessible": ("private.txt",)})
    tools = WorkspaceTools()
    tools.bind(request)
    assert "private.txt" not in tools.list_path(max_depth=3).content
    assert "private.txt" not in tools.search_path(".", "private").content
    operations: tuple[Callable[[], ToolOutput], ...] = (
        lambda: tools.read_file("private.txt"),
        lambda: tools.list_path("private.txt"),
        lambda: tools.search_path("private.txt", "private"),
        lambda: tools.write_file("overwrite", "private.txt", "changed"),
        lambda: tools.make_directory("private.txt"),
        lambda: tools.move_path("private.txt", "other.txt"),
        lambda: tools.move_path("prompt/system.md", "private.txt"),
        lambda: tools.delete_path("private.txt"),
    )
    for operation in operations:
        with pytest.raises(ToolError) as captured:
            operation()
        assert captured.value.code == "path_not_found"
        assert captured.value.message == "目标不存在"
    assert (request.workspace_dir / "private.txt").read_text() == "private\n"


def test_move_checks_destination_descendants(tmp_path: Path) -> None:
    """改名后的子路径也必须遵守目标权限。"""
    request = _request(tmp_path).model_copy(
        update={"inaccessible": ("archive/nested",)}
    )
    tools = WorkspaceTools()
    tools.bind(request)
    with pytest.raises(ToolError) as captured:
        tools.move_path("prompt", "archive")
    assert captured.value.code == "path_not_found"
    assert (request.workspace_dir / "prompt/nested/skill.md").is_file()
