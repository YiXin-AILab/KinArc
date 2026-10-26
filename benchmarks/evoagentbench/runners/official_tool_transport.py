"""保持 Nanobot 工具 Schema 不变，将执行传输到公开任务容器。

固定的 Nanobot 控制循环在私有进程中运行；模型控制的文件系统和
shell 参数仅在任务容器中解释，不在宿主执行。
"""

import asyncio
import importlib
import json
import math
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

FILE_TOOLS = {"read_file", "write_file", "edit_file", "list_dir"}
FILE_PROGRAM = """
import json, pathlib, sys
r=json.load(sys.stdin); a=r['params']; name=r['name']
p=pathlib.Path(a['path'])
try:
 if name=='read_file': print(p.read_text())
 elif name=='write_file':
  p.parent.mkdir(parents=True,exist_ok=True); p.write_text(a['content'])
  print('Successfully wrote file')
 elif name=='edit_file':
  text=p.read_text(); old=a['old_text']
  if not old or text.count(old)!=1: print('Error: old_text must match exactly once')
  else:
   p.write_text(text.replace(old,a['new_text'],1)); print('Successfully edited file')
 elif name=='list_dir': print('\\n'.join(str(x.name) for x in sorted(p.iterdir())))
except Exception as e: print('Error: '+str(e))
"""


def start_container(image: str, name: str) -> None:
    """创建新的非特权代码沙箱，不挂载宿主目录，也不开放网络。"""
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=1g",
            "--cpus=1",
            "--user=65534:65534",
            "--tmpfs=/workspace:rw,exec,size=256m,mode=1777,uid=65534,gid=65534",
            "--tmpfs=/tmp:rw,exec,size=128m,mode=1777,uid=65534,gid=65534",
            "--workdir=/workspace",
            "--env=HOME=/tmp",
            "--entrypoint=/bin/sh",
            image,
            "-c",
            "while :; do sleep 3600; done",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def execute_tool(
    environment: dict[str, str],
    name: str,
    params: dict[str, Any],
    *,
    timeout_seconds: float = 60,
) -> str:
    """仅通过 docker exec 参数执行固定文件操作或 shell 命令。"""
    if environment["kind"] == "research":
        return "Error: Research tasks permit only the official search MCP tools"
    if environment["kind"] != "knowledge":
        raise ValueError("Unsupported task environment kind")
    if name not in FILE_TOOLS | {"exec"}:
        return "Error: Tool disabled by the isolated benchmark transport"
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Tool timeout must be positive and finite")
    workspace = environment["workspace"]
    cwd = params.get("working_dir") or workspace
    base = ["docker", "exec", "-i", "--workdir", cwd, environment["container"]]
    if name == "exec":
        command = params["command"]
        supervisor = Path(__file__).with_name("command_supervisor.py").read_text()
        arguments = [*base, "python", "-c", supervisor]
        data = json.dumps(
            {"argv": ["/bin/bash", "-c", command], "timeout_seconds": timeout_seconds}
        )
    else:
        arguments = [*base, "python", "-c", FILE_PROGRAM]
        data = json.dumps({"name": name, "params": params})
    try:
        result = subprocess.run(
            arguments,
            input=data,
            text=True,
            capture_output=True,
            timeout=timeout_seconds + 10,
        )
    except subprocess.TimeoutExpired:
        # 仅限制 docker exec 的 CLI 超时不会终止容器内进程。
        subprocess.run(
            ["docker", "kill", environment["container"]],
            check=True,
            capture_output=True,
            timeout=30,
        )
        raise RuntimeError("Tool deadline invalidated the task container") from None
    if name == "exec":
        if result.returncode != 0:
            subprocess.run(
                ["docker", "kill", environment["container"]],
                check=True,
                capture_output=True,
                timeout=30,
            )
            raise RuntimeError("Command supervisor failed; task container invalidated")
        value = json.loads(result.stdout)
        output = str(value["stdout"]) + str(value["stderr"])
        if value["timed_out"]:
            return (
                output + f"\nError: Command timed out after {timeout_seconds:g}s; "
                "child processes terminated. Workspace preserved; you may retry "
                "with a smaller or corrected command."
            )
        return output + (
            f"\nExit code: {value['returncode']}" if value["returncode"] else ""
        )
    return (
        result.stdout
        + result.stderr
        + (f"\nExit code: {result.returncode}" if result.returncode else "")
    )[:64000]


def install_transport(environment: dict[str, str]) -> None:
    """仅替换工具派发方式，保留指定版本的 Agent 循环和工具 Schema。"""
    if environment["kind"] not in {"knowledge", "research"}:
        raise ValueError("Unsupported task environment kind")
    if environment["kind"] == "research":
        from mcp.client import sse, streamable_http

        original_http = streamable_http.streamable_http_client

        @asynccontextmanager
        async def research_client(
            url: str, *args: Any, **kwargs: Any
        ) -> AsyncIterator[tuple[Any, Any, Any]]:
            if url != environment["url"]:
                async with original_http(url, *args, **kwargs) as streams:
                    yield streams
                return
            # Nanobot 0.1.4.post3 将所有 URL 按 streamable HTTP 连接，而该 URL 对应的
            # 官方 BrowseComp-Plus 服务使用 MCP SSE。
            async with sse.sse_client(url, timeout=30, sse_read_timeout=3600) as pair:
                yield pair[0], pair[1], None

        streamable_http.streamable_http_client = research_client

    registry: Any = importlib.import_module("nanobot.agent.tools.registry")
    original = registry.ToolRegistry.execute

    async def execute(self: Any, name: str, params: dict[str, Any]) -> str:
        if environment["kind"] == "research" and name == "mcp_bcp-search_search":
            return str(await original(self, name, params))
        tool = self.get(name)
        if tool is None or tool.validate_params(params):
            return "Error: Unknown tool or invalid parameters"
        return await asyncio.to_thread(execute_tool, environment, name, params)

    registry.ToolRegistry.execute = execute
