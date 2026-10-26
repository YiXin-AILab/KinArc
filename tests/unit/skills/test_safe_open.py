"""跨平台 Skill 安全文件打开入口测试。"""

import os
from contextlib import suppress
from pathlib import Path

import pytest
from agentsystem.skill.safe_open import open_file_beneath_root


def test_opens_regular_file_beneath_root(tmp_path: Path) -> None:
    """根目录内普通文件应返回可读取且由调用方关闭的描述符。"""
    root = tmp_path / "skill"
    root.mkdir()
    target = root / "SKILL.md"
    target.write_text("content", encoding="utf-8")

    descriptor = open_file_beneath_root(root, target)
    try:
        assert os.read(descriptor, 7) == b"content"
    finally:
        os.close(descriptor)


def test_rejects_target_outside_root(tmp_path: Path) -> None:
    """根目录外目标不能通过路径解析后检查。"""
    root = tmp_path / "skill"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(OSError):
        open_file_beneath_root(root, outside)


def test_rejects_identity_change_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径身份与已打开文件不一致时必须关闭描述符并拒绝。"""
    root = tmp_path / "skill"
    root.mkdir()
    target = root / "SKILL.md"
    target.write_text("content", encoding="utf-8")
    opened_descriptors: list[int] = []
    original_open = os.open

    def tracked_open(
        path: Path | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os.path, "samestat", lambda _left, _right: False)

    try:
        with pytest.raises(OSError):
            open_file_beneath_root(root, target)
    finally:
        for descriptor in opened_descriptors:
            with suppress(OSError):
                os.close(descriptor)
