"""真实 Docker 生命周期测试，通过 EXECUTION_TEST_IMAGE 显式启用。"""

import asyncio
import os
import sys

import pytest
from agentsystem.common.errors import ToolError
from application.evoagentbench_knowledge_agent.plugins.docker import (
    DockerEnvironment,
    docker_call,
)
from application.evoagentbench_knowledge_agent.plugins.execution import execution_tools
from benchmarks.evoagentbench.common.process import invoke

pytestmark = pytest.mark.skipif(
    not os.environ.get("EXECUTION_TEST_IMAGE"), reason="Set EXECUTION_TEST_IMAGE"
)


@pytest.mark.asyncio
async def test_isolation_output_limit_and_cleanup() -> None:
    image = os.environ["EXECUTION_TEST_IMAGE"]
    first, second = DockerEnvironment(image), DockerEnvironment(image)
    try:
        await first.start()
        await second.start()
        result = await first.run("echo private > note; cat note", "/workspace", 5)
        assert result.stdout == "private\n"
        assert (await second.run("test ! -e note", "/workspace", 5)).exit_code == 0
        result = await first.run("yes x | head -c 100000", "/workspace", 5)
        assert result.truncated and len(result.stdout) == 64000
        result = await first.run("touch /forbidden", "/workspace", 5)
        assert result.exit_code != 0
    finally:
        await first.close()
        await second.close()
    assert (await docker_call(["inspect", first.name], 5)).exit_code != 0


@pytest.mark.asyncio
async def test_deadline_closes_container() -> None:
    env = DockerEnvironment(os.environ["EXECUTION_TEST_IMAGE"])
    try:
        await env.start()
        result = await env.run("sleep 60", "/workspace", 0.1)
        assert result.timed_out and result.exit_code == 124
        with pytest.raises(ToolError):
            await env.run("echo should-not-run", "/workspace", 1)
        assert (await docker_call(["inspect", env.name], 5)).exit_code != 0
    finally:
        await env.close()


@pytest.mark.asyncio
async def test_cancellation_closes_container() -> None:
    env = DockerEnvironment(os.environ["EXECUTION_TEST_IMAGE"])
    try:
        await env.start()
        task = asyncio.create_task(env.run("sleep 60", "/workspace", 30))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await docker_call(["inspect", env.name], 5)).exit_code != 0
    finally:
        await env.close()


@pytest.mark.asyncio
async def test_parent_supervisor_reclaims_container_after_worker_timeout() -> None:
    env = DockerEnvironment(os.environ["EXECUTION_TEST_IMAGE"])
    try:
        await env.start()
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(
                invoke,
                [sys.executable, "-c", "import time; time.sleep(30)"],
                {},
                timeout=0.1,
                owned_container=env.name,
            )
        assert (await docker_call(["inspect", env.name], 5)).exit_code != 0
    finally:
        await env.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("EXECUTION_PYTHON_IMAGE"), reason="Requires Python image"
)
async def test_files_and_python_share_real_container_workspace() -> None:
    env = DockerEnvironment(os.environ["EXECUTION_PYTHON_IMAGE"])
    try:
        await env.start()
        tools = {t.name: t for t in execution_tools(env)}
        await tools["write_file"].handler.invoke(
            ctx=None, arguments={"path": "solve.py", "content": "print(6 * 7)"}
        )
        result = await env.run("python3 solve.py", "/workspace", 5)
        assert result.stdout == "42\n"
        await tools["replace_text"].handler.invoke(
            ctx=None,
            arguments={"path": "solve.py", "old_text": "6 * 7", "new_text": "40 + 2"},
        )
        assert (await env.run("python3 solve.py", "/workspace", 5)).stdout == "42\n"
        assert (await env.run("rg '40' solve.py", "/workspace", 5)).exit_code == 0
        assert (await env.run("g++ --version", "/workspace", 5)).exit_code == 0
    finally:
        await env.close()
