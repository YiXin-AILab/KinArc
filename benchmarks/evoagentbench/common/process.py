"""供两套依赖环境共用的有界子进程监督逻辑。"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import Any, TextIO
from uuid import uuid4


def _save_diagnostic(
    error: BaseException, returncode: int | None, diagnostics: TextIO
) -> None:
    """保存安全的失败信息，不复制任意 worker 标准输出或密钥。"""
    diagnostics.flush()
    diagnostics.seek(0)
    frames = []
    worker_error = None
    for line in diagnostics:
        frame = re.fullmatch(r'  File "([^"]+)", line (\d+), in ([\w<>]+)\n?', line)
        if frame:
            frames.append(
                {
                    "file": Path(frame[1]).name,
                    "line": int(frame[2]),
                    "function": frame[3],
                }
            )
        kind = re.match(r"^([A-Za-z]+(?:Error|Exception)):", line)
        if kind:
            worker_error = kind[1]
    summary = {
        "exception_type": type(error).__name__,
        "returncode": returncode,
        "worker_exception_type": worker_error,
        "frames": frames[-32:],
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="eab-diagnostic-",
        suffix=".json",
        delete=False,
    ) as saved:
        json.dump(summary, saved)
    # 结构化信息可写入持久化试次归档，无需复制标准输出。
    vars(error)["process_diagnostic"] = summary
    error.diagnostic_path = saved.name  # type: ignore[attr-defined]  # 内置异常支持附加属性。
    error.add_note(f"Private diagnostic: {saved.name}")


def remove_owned_container(name: str) -> None:
    """容器已不存在时视为清理完成；清理失败必须保持可观测。"""
    result = subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode and "no such container" not in result.stderr.lower():
        raise RuntimeError(
            f"Failed to remove owned container {name}: {result.stderr[:2000]}"
        )


def _cleanup(name: str, *, canceling: bool) -> None:
    try:
        remove_owned_container(name)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        if not canceling:
            raise
        logging.getLogger(__name__).exception(
            "Container cleanup failed while propagating cancellation"
        )


def invoke(
    command: list[str],
    payload: dict[str, Any],
    *,
    timeout: float,
    cancel: Event | None = None,
    container_image: str | None = None,
    mounts: tuple[Path, ...] = (),
    owned_container: str | None = None,
) -> dict[str, Any]:
    """使用私有文件交换数据，回收进程组，不打印请求内容。

    Args:
        command: 接受 --request 和 --result 文件参数的 worker 命令。
        payload: 通过临时文件传递的私有 JSON 请求。
        timeout: worker 生命周期的硬超时秒数。
        cancel: worker 执行期间检查的可选协作停止信号。
        container_image: 在新沙箱中执行 worker 时使用的可选镜像。
        mounts: 仅供可选 worker 沙箱使用的宿主只读挂载目录。
        owned_container: worker 完成后需要清理的可选自有任务容器。

    Returns:
        成功完成的 worker 返回并解析后的 JSON 结果。

    Raises:
        TimeoutError: worker 执行超过硬超时。
        asyncio.CancelledError: 协作停止信号已设置。
        RuntimeError: worker 以非零状态退出，或容器清理失败。
        ValueError: worker 结果不是 JSON 对象。
    """
    if cancel is not None and cancel.is_set():
        raise asyncio.CancelledError
    with tempfile.TemporaryDirectory(prefix="eab-") as directory:
        request = Path(directory) / "request.json"
        result = Path(directory) / "result.json"
        request.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        container_name = f"eab-{uuid4().hex}" if container_image else None
        if container_image:
            mount_args = [
                value
                for path in mounts
                for value in ("--mount", f"type=bind,src={path},dst={path},readonly")
            ]
            command = [
                "docker",
                "run",
                "--rm",
                "--name",
                str(container_name),
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "256",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--tmpfs",
                "/tmp:rw,size=512m",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "HOME=/tmp",
                *mount_args,
                "--mount",
                f"type=bind,src={directory},dst=/exchange",
                container_image,
                *command,
            ]
        request_arg = "/exchange/request.json" if container_image else str(request)
        result_arg = "/exchange/result.json" if container_image else str(result)
        with (Path(directory) / "diagnostics.log").open("w+") as diagnostics:
            child = subprocess.Popen(
                [*command, "--request", request_arg, "--result", result_arg],
                stdout=diagnostics,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout
                while child.poll() is None:
                    if cancel is not None and cancel.is_set():
                        raise asyncio.CancelledError
                    if time.monotonic() >= deadline:
                        raise TimeoutError("EvoAgentBench subprocess deadline exceeded")
                    time.sleep(0.05)
                if child.returncode != 0:
                    raise RuntimeError(
                        f"EvoAgentBench subprocess failed (exit={child.returncode})"
                    )
                value = json.loads(result.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("Worker response must be an object")
                return value
            except BaseException as error:
                _save_diagnostic(error, child.poll(), diagnostics)
                raise
            finally:
                # 即使直接 worker 已退出，也要终止其后代进程。
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
                cleanup_name = container_name or owned_container
                if cleanup_name:
                    _cleanup(
                        cleanup_name,
                        canceling=isinstance(sys.exception(), asyncio.CancelledError),
                    )
