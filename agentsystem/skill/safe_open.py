"""跨平台 Skill 资源安全打开入口。"""

import os
import stat
from pathlib import Path


def open_file_beneath_root(root: Path, target: Path) -> int:
    """校验解析后的路径归属和文件身份，返回已打开的只读描述符。

    Args:
        root: 必须已存在的允许根目录。
        target: 必须解析到 root 内普通文件的目标路径。

    Returns:
        调用方负责关闭的文件描述符；校验失败时由本函数关闭。

    Raises:
        OSError: 路径不存在、越界、不是普通文件或打开后的身份不一致。
        RuntimeError: 路径解析失败，例如部分平台上的符号链接循环。
    """
    resolved_root = root.resolve(strict=True)
    resolved_target = target.resolve(strict=True)
    if resolved_target == resolved_root or not resolved_target.is_relative_to(
        resolved_root
    ):
        raise OSError(f"Skill 路径越界：{target}")

    descriptor = os.open(
        resolved_target,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )
    try:
        opened = os.fstat(descriptor)
        current = resolved_target.stat()
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, current):
            raise OSError(f"Skill 文件不安全：{target}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
