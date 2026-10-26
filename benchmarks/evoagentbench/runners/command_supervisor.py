"""仅适用于 Linux 的命令监督器，以源码形式发送到公开任务容器中。

子进程回收器同时管理初始 shell 和脱离会话的后代进程；模型生成的
命令不在宿主执行。清理失败时监督器以非零状态退出，
使宿主将沙箱标记为失效，避免误报已安全恢复。
"""

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path


def descendants() -> set[int]:
    """读取监督器当前的后代进程树，包括接管的孤儿进程。"""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm 可能包含空格或括号；最后一个 ')' 后的字段
            # 依次为 state、ppid。
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            parents[int(entry.name)] = int(fields[1])
        except FileNotFoundError:
            continue
    owned = {os.getpid()}
    while True:
        expanded = owned | {pid for pid, parent in parents.items() if parent in owned}
        if expanded == owned:
            return owned - {os.getpid()}
        owned = expanded


def cleanup() -> None:
    """终止并回收全部自有子进程；无法完成清理时停止后续执行。"""
    deadline = time.monotonic() + 3
    while True:
        remaining = descendants()
        for pid in remaining:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if not descendants():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Command descendants could not be cleaned up")
        time.sleep(0.02)


def main() -> None:
    """按内部时限执行一条命令，并返回安全的结构化结果。"""
    request = json.load(sys.stdin)
    # PR_SET_CHILD_SUBREAPER：双重 fork 或 setsid 后代进程会重新归属当前进程。
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("Cannot enable command subreaper")
    timed_out = False
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        child = subprocess.Popen(
            request["argv"],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            try:
                returncode = child.wait(timeout=request["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = 124
        finally:
            cleanup()
            child.wait()
        stdout.seek(0)
        stderr.seek(0)
        print(
            json.dumps(
                {
                    "stdout": stdout.read(32000).decode(errors="replace"),
                    "stderr": stderr.read(32000).decode(errors="replace"),
                    "returncode": returncode,
                    "timed_out": timed_out,
                }
            )
        )


if __name__ == "__main__":
    main()
