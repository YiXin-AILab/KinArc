"""真实进程组、取消与监听 socket 的所有权边界。"""

import asyncio
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from evolution_tasks.tau3_banking._internal import process


def test_stop_process_reaps_descendants(tmp_path: Path) -> None:
    """关闭必须覆盖同组子进程，不能只 terminate 直接父进程。"""
    ready = tmp_path / "ready"
    program = """
import subprocess, sys, signal, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
def stop(*args):
    child.wait(timeout=3)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
Path(sys.argv[1]).write_text(str(child.pid))
while True: time.sleep(.05)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", program, str(ready)], start_new_session=True
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            threading.Event().wait(0.01)
        assert ready.exists()
        child_pid = int(ready.read_text())
        process._stop_process(parent, 2)
        assert parent.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid, signal.SIGKILL)
            parent.wait(timeout=3)


def test_stop_process_escalates_to_kill(tmp_path: Path) -> None:
    """忽略 TERM 的拥有进程必须在有限时间内被 KILL。"""
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time,sys; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "Path(sys.argv[1]).touch(); time.sleep(60)",
            str(ready),
        ],
        start_new_session=True,
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            threading.Event().wait(0.01)
        assert ready.exists()
        process._stop_process(child, 0.1)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


def test_wait_process_observes_cancellation() -> None:
    """运行中取消不必等 case 的长超时。"""
    stop = threading.Event()
    stop.set()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            process._wait_process(child, 60, stop)
    finally:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=3)


@pytest.mark.parametrize("disappears", [True, False])
def test_group_permission_probe_never_means_stopped(
    monkeypatch: pytest.MonkeyPatch,
    disappears: bool,
) -> None:
    """EPERM 表示尚不能确认消失；短暂状态应重查，持续状态必须失败。"""
    from agentsystem.common.errors import AdapterError

    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    child.wait(timeout=3)
    probes = 0

    def killpg(pid: int, action: int) -> None:
        nonlocal probes
        assert pid == child.pid
        if action == 0:
            probes += 1
            if disappears and probes > 2:
                raise ProcessLookupError
        raise PermissionError

    monkeypatch.setattr(os, "killpg", killpg)
    if disappears:
        process._stop_process(child, 0.05)
        assert probes > 2
    else:
        with pytest.raises(AdapterError, match="未能安全回收"):
            process._stop_process(child, 0.05)


def test_preflight_rejects_unprepared_tau2_environment(tmp_path: Path) -> None:
    """缺少解释器在任何 case 启动前失败，不隐式安装。"""
    from tests.unit.evolution_tasks.tau3_banking.test_process import _config

    config = _config(tmp_path)
    with pytest.raises(ValueError, match="uv sync"):
        process.validate_execution_environment(config, tmp_path, tmp_path)
