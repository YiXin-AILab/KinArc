"""使用真实 Docker 验证基线工具无法检查宿主私有证据。"""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from benchmarks.evoagentbench.runners.official_tool_transport import (
    execute_tool,
    start_container,
)


@pytest.mark.skipif(
    os.environ.get("EAB_RUN_DOCKER") != "1", reason="opt-in Docker isolation"
)
def test_baseline_tools_cannot_read_host_files_or_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实宿主诱饵文件和供应商环境变量不得进入任务容器。"""
    secret = "decoy-" + uuid4().hex
    decoy = tmp_path / "hidden-tests.json"
    decoy.write_text(secret)
    monkeypatch.setenv("EAB_API_KEY", secret)
    name = "eab-isolation-" + uuid4().hex
    environment = {"kind": "knowledge", "container": name, "workspace": "/workspace"}
    try:
        start_container("ea-bench-execution:local", name)
        for tool, args in (
            ("read_file", {"path": str(decoy)}),
            ("exec", {"command": f"cat {decoy}; env; ls /var/run/docker.sock"}),
        ):
            result = execute_tool(environment, tool, args)
            assert secret not in result
            assert "No such file" in result
        assert "Successfully" in execute_tool(
            environment,
            "write_file",
            {"path": "/workspace/public.txt", "content": "public"},
        )
        assert (
            execute_tool(
                environment, "read_file", {"path": "/workspace/public.txt"}
            ).strip()
            == "public"
        )
        assert "Error:" in execute_tool(
            {"kind": "research"}, "exec", {"command": "env"}
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], check=False, capture_output=True, timeout=30
        )


@pytest.mark.skipif(
    os.environ.get("EAB_RUN_DOCKER") != "1", reason="opt-in Docker isolation"
)
def test_timeout_cleans_descendants_and_preserves_workspace() -> None:
    """忽略信号且脱离会话的孙进程不能在可恢复超时后继续存活。"""
    name = "eab-timeout-" + uuid4().hex
    environment = {"kind": "knowledge", "container": name, "workspace": "/workspace"}
    try:
        start_container("ea-bench-execution:local", name)
        command = """echo saved > kept.txt
python -c 'import os,signal,time
os.setsid()
signal.signal(signal.SIGTERM,signal.SIG_IGN)
open("child.pid","w").write(str(os.getpid()))
time.sleep(30)
open("leaked","w").write("bad")' &
wait
"""
        result = execute_tool(
            environment, "exec", {"command": command}, timeout_seconds=1
        )
        assert "timed out" in result
        result = execute_tool(
            environment,
            "exec",
            {
                "command": (
                    "cat kept.txt; if kill -0 $(cat child.pid) 2>/dev/null; "
                    "then echo LEAK; fi; echo recovered"
                )
            },
        )
        assert "saved" in result and "recovered" in result and "LEAK" not in result
        result = execute_tool(environment, "exec", {"command": "echo normal; exit 7"})
        assert "normal" in result and "Exit code: 7" in result
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


@pytest.mark.skipif(
    os.environ.get("EAB_RUN_DOCKER") != "1", reason="opt-in Docker isolation"
)
def test_broken_supervisor_invalidates_container() -> None:
    """监督器本身被终止时，不得返回可恢复的超时结果。"""
    name = "eab-supervisor-" + uuid4().hex
    environment = {"kind": "knowledge", "container": name, "workspace": "/workspace"}
    try:
        start_container("ea-bench-execution:local", name)
        with pytest.raises(RuntimeError, match="supervisor failed"):
            execute_tool(environment, "exec", {"command": "kill -9 $PPID; sleep 30"})
        state = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            text=True,
            timeout=30,
        )
        assert state.strip() == "false"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
