"""Evolution Agent 请求级 Workspace 文件工具。"""

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Literal

from agentsystem.common.errors import ToolError
from agentsystem.tool.models import ToolOutput

from evolution.improver import ImprovementRequest
from evolution.workspace import WorkspaceAccess

_MAX_WRITE_BYTES = 262_144


class WorkspaceTools:
    """将文件操作限制在当前请求的可见 Staging Workspace。"""

    def __init__(self) -> None:
        """初始化未绑定请求的工作区工具服务。"""
        self._request: ImprovementRequest | None = None

    def bind(self, request: ImprovementRequest) -> None:
        """绑定当前唯一可见的改进请求。"""
        self._request = request

    def unbind(self) -> None:
        """清除本轮请求，避免证据跨轮残留。"""
        self._request = None

    def list_path(self, path: str = ".", max_depth: int = 1) -> ToolOutput:
        """列出目录内容，并按 max_depth 递归展开。"""
        if max_depth < 1:
            raise ToolError(code="invalid_limit", message="max_depth 必须大于 0")
        relative, directory = self._path(path, allow_root=True)
        self._require_directory(directory)
        entries: list[dict[str, str | int]] = []
        self._collect_entries(directory, relative, max_depth, entries)
        return _output({"path": relative or ".", "entries": entries})

    def read_file(
        self,
        path: str,
        offset: int = 0,
        limit: int = 200,
    ) -> ToolOutput:
        """按行读取 UTF-8 文本文件。"""
        if offset < 0:
            raise ToolError(code="invalid_offset", message="offset 不得为负数")
        if limit < 1:
            raise ToolError(code="invalid_limit", message="limit 必须大于 0")
        relative, candidate = self._path(path, allow_changelog=True)
        self._require_file(candidate)
        lines, eof = _read_lines(candidate, offset=offset, limit=limit)
        parts = [f"--- {relative}"]
        for number, line in enumerate(lines, start=offset + 1):
            parts.append(f"{number}\t" + line.removesuffix("\n"))
        parts.append(f"--- next_offset={offset + len(lines)} eof={str(eof).lower()}")
        return ToolOutput(content="\n".join(parts))

    def search_path(
        self,
        path: str,
        query: str,
        case_sensitive: bool = False,
        limit: int = 20,
    ) -> ToolOutput:
        """在文件或目录中递归搜索字面文本。"""
        if not query:
            raise ToolError(code="invalid_query", message="query 不能为空")
        if limit < 1:
            raise ToolError(code="invalid_limit", message="limit 必须大于 0")
        _, candidate = self._path(path, allow_root=True, allow_changelog=True)
        files = self._search_files(candidate)
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, str | int]] = []
        for file_path in files:
            try:
                with file_path.open(encoding="utf-8") as source:
                    for line_number, line in enumerate(source, start=1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle not in haystack:
                            continue
                        matches.append(
                            {
                                "path": self._relative(file_path),
                                "line": line_number,
                                "text": line.rstrip("\r\n"),
                            }
                        )
                        if len(matches) == limit:
                            return _search_output(matches, limit)
            except UnicodeDecodeError as error:
                raise ToolError(
                    code="unsupported_file_type",
                    message=f"文件不是 UTF-8 文本：{self._relative(file_path)}",
                ) from error
        return _search_output(matches, limit)

    def write_file(
        self,
        action: Literal["create", "overwrite", "replace", "append", "prepend"],
        path: str,
        new_content: str,
        old_content: str | None = None,
        replace_all: bool = True,
    ) -> ToolOutput:
        """创建、覆盖、前置、追加或精确替换 UTF-8 文本文件。"""
        encoded = new_content.encode("utf-8")
        _check_write_size(encoded)
        relative, candidate = self._path(path, allow_changelog=True)
        is_changelog = candidate == self._bound_request().changelog_path
        if not is_changelog:
            self._require_writable(relative)
        replacements: int | None = None
        if action == "create":
            if candidate.exists():
                raise ToolError(code="path_already_exists", message="目标已经存在")
            self._create_parents(candidate, relative)
        elif action in {"overwrite", "replace", "append", "prepend"}:
            self._require_file(candidate)
            if action == "replace":
                new_content, replacements = self._replace_content(
                    candidate,
                    old_content,
                    new_content,
                    replace_all,
                )
            elif action in {"append", "prepend"}:
                existing = self._read_text(candidate)
                new_content = (
                    new_content + existing
                    if action == "prepend"
                    else existing + new_content
                )
            encoded = new_content.encode("utf-8")
            if not is_changelog:
                _check_write_size(encoded)
        else:
            raise ToolError(code="invalid_action", message="action 不受支持")
        if candidate.exists() and candidate.read_bytes() == encoded:
            raise ToolError(code="no_change", message="写入不会产生内容变化")
        _atomic_write(candidate, encoded)
        result: dict[str, str | int] = {
            "path": relative,
            "action": action,
            "size_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
        if replacements is not None:
            result["replacements"] = replacements
        return _output(result)

    def make_directory(self, path: str, recursive: bool = False) -> ToolOutput:
        """创建目录，并按需递归创建父目录。"""
        relative, candidate = self._path(path)
        self._require_writable(relative)
        if candidate.exists():
            self._require_directory(candidate)
            return _output({"path": relative})
        parent = candidate.parent
        if recursive:
            self._create_parents(candidate, relative, include_candidate=True)
        else:
            if not parent.exists():
                raise ToolError(code="path_not_found", message="父目录不存在")
            self._require_directory(parent)
            candidate.mkdir()
        return _output({"path": relative})

    def move_path(self, source: str, destination: str) -> ToolOutput:
        """移动或重命名文件或目录，不覆盖已有目标。"""
        source_relative, source_path = self._path(source)
        destination_relative, destination_path = self._path(destination)
        self._require_unprotected_tree(source_relative)
        self._require_writable(destination_relative)
        source_type = self._entry_type(source_path)
        if source_type == "directory":
            self._validate_tree(source_path)
            if destination_relative.startswith(source_relative + "/"):
                raise ToolError(code="invalid_move", message="目录不能移动到自身内部")
            self._require_destination_tree_writable(
                source_path,
                source_relative,
                destination_relative,
            )
        if destination_path.exists():
            raise ToolError(code="destination_exists", message="目标已经存在")
        self._create_parents(destination_path, destination_relative)
        shutil.move(source_path, destination_path)
        return _output({"source": source_relative, "destination": destination_relative})

    def delete_path(self, path: str, recursive: bool = False) -> ToolOutput:
        """删除文件或目录，非空目录要求 recursive=true。"""
        relative, candidate = self._path(path)
        self._require_unprotected_tree(relative)
        entry_type = self._entry_type(candidate)
        if entry_type == "file":
            candidate.unlink()
        else:
            self._validate_tree(candidate)
            if any(candidate.iterdir()) and not recursive:
                raise ToolError(
                    code="directory_not_empty",
                    message="非空目录要求 recursive=true",
                )
            if recursive:
                shutil.rmtree(candidate)
            else:
                candidate.rmdir()
        return _output({"path": relative})

    def _bound_request(self) -> ImprovementRequest:
        """取得当前绑定请求；未绑定时拒绝文件访问。"""
        if self._request is None:
            raise ToolError(code="request_not_bound", message="尚未绑定改进请求")
        return self._request

    def _path(
        self, raw_path: str, *, allow_root: bool = False, allow_changelog: bool = False
    ) -> tuple[str, Path]:
        """解析可见工作区路径，拒绝路径穿越、符号链接及不可访问项。"""
        request = self._bound_request()
        if allow_changelog and raw_path == str(request.changelog_path):
            return raw_path, self._changelog_path()
        parts = _path_parts(raw_path, allow_root=allow_root)
        if self._policy().is_inaccessible("/".join(parts)):
            raise ToolError(code="path_not_found", message="目标不存在")
        workspace = request.workspace_dir
        if workspace.is_symlink():
            raise ToolError(
                code="symlink_forbidden", message="Workspace 不得是符号链接"
            )
        if not workspace.is_dir():
            raise ToolError(code="path_not_found", message="Workspace 不存在")
        candidate = workspace.joinpath(*parts)
        current = workspace
        for index, part in enumerate(parts):
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                break
            if stat.S_ISLNK(mode):
                raise ToolError(
                    code="symlink_forbidden", message="路径不得包含符号链接"
                )
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                raise ToolError(code="not_directory", message="路径父级不是目录")
        return "/".join(parts), candidate

    def _changelog_path(self) -> Path:
        """验证请求指定的精确日志绝对路径及其各级目录。"""
        log = self._bound_request().changelog_path
        if not log.is_absolute() or ".." in log.parts:
            raise ToolError(code="invalid_path", message="日志必须使用精确绝对路径")
        for ancestor in reversed(log.parents):
            self._require_directory(ancestor)
        self._require_file(log)
        return log

    def _relative(self, path: Path) -> str:
        """返回工具可见路径；指定 Changelog 保留绝对路径。"""
        request = self._bound_request()
        if path == request.changelog_path:
            return str(path)
        return path.relative_to(request.workspace_dir).as_posix()

    def _entry_type(self, path: Path) -> Literal["file", "directory"]:
        """使用 lstat 识别普通文件或目录，拒绝链接与特殊文件。"""
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as error:
            raise ToolError(code="path_not_found", message="目标不存在") from error
        if stat.S_ISLNK(mode):
            raise ToolError(code="symlink_forbidden", message="目标不得是符号链接")
        if stat.S_ISREG(mode):
            return "file"
        if stat.S_ISDIR(mode):
            return "directory"
        raise ToolError(code="unsupported_file_type", message="目标不是普通文件或目录")

    def _require_file(self, path: Path) -> None:
        """要求路径指向普通文件，否则抛出 ToolError。"""
        if self._entry_type(path) != "file":
            raise ToolError(code="not_file", message="目标不是文件")

    def _require_directory(self, path: Path) -> None:
        """要求路径指向真实目录，否则抛出 ToolError。"""
        if self._entry_type(path) != "directory":
            raise ToolError(code="not_directory", message="目标不是目录")

    def _collect_entries(
        self,
        directory: Path,
        relative: str,
        depth: int,
        entries: list[dict[str, str | int]],
    ) -> None:
        """按名称排序收集指定深度内的可见目录项。"""
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if self._policy().is_inaccessible(self._relative(child)):
                continue
            child_type = self._entry_type(child)
            child_relative = f"{relative}/{child.name}" if relative else child.name
            entry: dict[str, str | int] = {
                "path": child_relative,
                "type": child_type,
            }
            if child_type == "file":
                entry["size_bytes"] = child.stat().st_size
            entries.append(entry)
            if child_type == "directory" and depth > 1:
                self._collect_entries(child, child_relative, depth - 1, entries)

    def _search_files(self, candidate: Path) -> tuple[Path, ...]:
        """枚举可搜索的普通文件，跳过不可访问路径并拒绝链接。"""
        entry_type = self._entry_type(candidate)
        if entry_type == "file":
            return (candidate,)
        files: list[Path] = []
        for current, directories, filenames in os.walk(candidate, followlinks=False):
            current_path = Path(current)
            visible_directories: list[str] = []
            for name in directories:
                if self._policy().is_inaccessible(self._relative(current_path / name)):
                    continue
                self._entry_type(current_path / name)
                visible_directories.append(name)
            directories[:] = visible_directories
            for name in filenames:
                if self._policy().is_inaccessible(self._relative(current_path / name)):
                    continue
                path = current_path / name
                if self._entry_type(path) == "file":
                    files.append(path)
        return tuple(sorted(files, key=self._relative))

    def _validate_tree(self, root: Path) -> None:
        """在整树操作前校验所有后代的类型及写权限。"""
        self._require_directory(root)
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *filenames):
                if self._policy().is_inaccessible(self._relative(current_path / name)):
                    raise ToolError(
                        code="path_not_found",
                        message="目标不存在",
                    )
                self._require_writable(self._relative(current_path / name))
                self._entry_type(current_path / name)

    def _require_writable(self, relative: str) -> None:
        """检查目标及其祖先，拒绝不可访问或只读路径。"""
        if self._policy().is_inaccessible(relative):
            raise ToolError(code="path_not_found", message="目标不存在")
        for candidate in _ancestors(relative):
            if self._is_protected(candidate):
                raise ToolError(code="protected_path", message="目标路径受保护")

    def _require_unprotected_tree(self, relative: str) -> None:
        """拒绝会移动或删除只读路径及其父目录的操作。"""
        self._require_writable(relative)
        protected = self._bound_request().read_only
        for raw in protected:
            base = raw.rstrip("/")
            if (
                relative == base
                or relative.startswith(base + "/")
                or base.startswith(relative + "/")
            ):
                raise ToolError(code="protected_path", message="目标路径影响保护边界")

    def _policy(self) -> WorkspaceAccess:
        """从当前请求构造只读与不可访问路径策略。"""
        request = self._bound_request()
        return WorkspaceAccess(
            read_only=request.read_only, inaccessible=request.inaccessible
        )

    def _is_protected(self, relative: str) -> bool:
        """判断相对路径是否落入当前请求的只读范围。"""
        return self._policy().is_read_only(relative)

    def _create_parents(
        self,
        candidate: Path,
        relative: str,
        *,
        include_candidate: bool = False,
    ) -> None:
        """校验祖先写权限后创建缺失目录，可包含目标目录自身。"""
        parent_relative = relative if include_candidate else relative.rpartition("/")[0]
        for ancestor in _ancestors(parent_relative):
            self._require_writable(ancestor)
        target = candidate if include_candidate else candidate.parent
        target.mkdir(parents=True, exist_ok=True)

    def _replace_content(
        self,
        candidate: Path,
        old_content: str | None,
        new_content: str,
        replace_all: bool,
    ) -> tuple[str, int]:
        """计算精确文本替换后的内容及替换次数，不直接写入文件。"""
        if not old_content:
            raise ToolError(code="invalid_content", message="old_content 不得为空")
        current = self._read_text(candidate)
        occurrences = current.count(old_content)
        if occurrences == 0:
            raise ToolError(code="text_not_found", message="找不到 old_content")
        replacements = occurrences if replace_all else 1
        return current.replace(
            old_content, new_content, -1 if replace_all else 1
        ), replacements

    def _read_text(self, candidate: Path) -> str:
        """读取 UTF-8 文本，将解码失败转换为工具错误。"""
        try:
            return candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolError(
                code="unsupported_file_type",
                message=f"文件不是 UTF-8 文本：{self._relative(candidate)}",
            ) from error

    def _require_destination_tree_writable(
        self,
        source_path: Path,
        source_relative: str,
        destination_relative: str,
    ) -> None:
        """按移动后的路径逐项验证目标目录树可写。"""
        for path in source_path.rglob("*"):
            suffix = self._relative(path).removeprefix(source_relative + "/")
            self._require_writable(f"{destination_relative}/{suffix}")


def _check_write_size(content: bytes) -> None:
    """在写盘前拒绝超过单次写入字节上限的内容。"""
    if len(content) > _MAX_WRITE_BYTES:
        raise ToolError(code="content_too_large", message="写入内容超过 256 KiB")


def _path_parts(raw_path: str, *, allow_root: bool) -> tuple[str, ...]:
    """解析 POSIX 相对路径，按调用约定允许根目录表示。"""
    if raw_path == "." and allow_root:
        return ()
    if not raw_path or raw_path.startswith("/") or "\\" in raw_path:
        raise ToolError(code="invalid_path", message="路径必须是 POSIX 相对路径")
    parts = tuple(raw_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ToolError(code="invalid_path", message="路径包含非法路径段")
    return parts


def _ancestors(relative: str) -> tuple[str, ...]:
    """返回从首级目录到目标自身的相对路径前缀。"""
    if not relative:
        return ()
    parts = relative.split("/")
    return tuple("/".join(parts[:index]) for index in range(1, len(parts) + 1))


def _read_lines(path: Path, *, offset: int, limit: int) -> tuple[list[str], bool]:
    """按零起始偏移读取文本页，同时返回是否到达文件末尾。"""
    selected: list[str] = []
    eof = True
    try:
        with path.open(encoding="utf-8") as source:
            for index, line in enumerate(source):
                if index < offset:
                    continue
                if len(selected) == limit:
                    eof = False
                    break
                selected.append(line)
    except UnicodeDecodeError as error:
        raise ToolError(
            code="unsupported_file_type", message="文件不是 UTF-8 文本"
        ) from error
    return selected, eof


def _atomic_write(path: Path, content: bytes) -> None:
    """通过同目录临时文件原子替换内容，并保留已有文件权限。"""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            if path.exists():
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _search_output(matches: list[dict[str, str | int]], limit: int) -> ToolOutput:
    """按文件分组渲染命中行，并提示是否达到返回上限。"""
    parts: list[str] = []
    previous_path: str | int | None = None
    for match in matches:
        if match["path"] != previous_path:
            parts.append(f"--- {match['path']}")
            previous_path = match["path"]
        parts.append(f"{match['line']}\t{match['text']}")
    parts.append(f"--- matches={len(matches)} limit={limit}")
    if len(matches) == limit:
        parts.append("--- 已达到匹配数量上限，可能还有其他匹配。")
    return ToolOutput(content="\n".join(parts))


def _output(value: object) -> ToolOutput:
    """将文件操作结果编码为 JSON 工具响应。"""
    return ToolOutput(content=json.dumps(value, ensure_ascii=False))


__all__ = ["WorkspaceTools"]
