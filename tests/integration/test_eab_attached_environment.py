"""显式启用的 Docker 测试，验证办公任务容器的超时协议。"""

import os

import pytest
from application.evoagentbench_knowledge_agent.plugins.docker import (
    DockerEnvironment,
    docker_call,
)
from benchmarks.evoagentbench.domains.domain_environment import AttachedContainer


@pytest.mark.skipif(
    not os.environ.get("EAB_EXECUTION_IMAGE"),
    reason="Set EAB_EXECUTION_IMAGE to a prepared local image",
)
async def test_office_timeout_kills_container_when_command_ignores_term() -> None:
    owner = DockerEnvironment(os.environ["EAB_EXECUTION_IMAGE"])
    await owner.start()
    try:
        attached = AttachedContainer(owner.name)
        result = await attached.run(
            "trap '' TERM; while :; do sleep 1; done", "/workspace", 0.2
        )
        assert result.exit_code in {124, 137}
        state = await docker_call(
            ["inspect", "--format", "{{.State.Running}}", owner.name], 5
        )
        assert state.stdout.strip() == "false"
        with pytest.raises(RuntimeError, match="invalidated"):
            await attached.run("echo cannot_run", "/workspace", 1)
    finally:
        await owner.close()
