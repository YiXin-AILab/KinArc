"""显式 opt-in：真实 Tau2/LiteLLM SDK 的用户请求超时与重试边界。"""

import os
import subprocess
from pathlib import Path

import pytest


def test_user_timeout_reaches_http_and_retries_same_prompt(tmp_path: Path) -> None:
    """Runner 的 180 秒必须到达 HTTP；读超时后重试同一条用户生成请求。"""
    dependency = os.environ.get("TEST_TAU2_PROJECT")
    if not dependency:
        pytest.skip("需要显式 TEST_TAU2_PROJECT")
    script = tmp_path / "user_timeout.py"
    script.write_text(_PROGRAM, encoding="utf-8")
    result = subprocess.run(
        [str(Path(dependency) / ".venv/bin/python"), str(script)],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).parents[4]),
            "PYTHON_DOTENV_DISABLED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "OPENAI_API_KEY": "local-fixture",
            "OPENAI_BASE_URL": "http://user.test/v1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


_PROGRAM = """
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import litellm
from tau2.data_model.message import UserMessage
from tau2.utils.llm_utils import generate
from evolution_tasks.tau3_banking._internal import runner
from evolution_tasks.tau3_banking._internal.process import RunnerRequest

litellm.telemetry = False
requests = []

def send(self, request):
    assert request.url.host == "user.test"
    requests.append(request)
    assert request.extensions["timeout"]["read"] == 180
    if len(requests) == 1:
        raise httpx.ReadTimeout("injected user timeout", request=request)
    return httpx.Response(200, request=request, json={
        "id": "reply", "object": "chat.completion", "created": 1,
        "model": "gpt-4o-mini", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "user reply"},
            "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    })

httpx.HTTPTransport.handle_request = send

def build(config, task):
    def run():
        reply = generate(model=config.llm_user,
            messages=[UserMessage(role="user", content="next user reply")],
            **config.llm_args_user)
        return SimpleNamespace(duration=0.1, reward_info=None,
            model_dump=lambda **kw: {"reply": reply.content})
    return SimpleNamespace(run=run)

attribute = runner._attribute
def resolve(module, name):
    if name == "build_text_orchestrator":
        return build
    if name == "registry":
        return SimpleNamespace(register_agent_factory=lambda *args: None)
    return attribute(module, name)

runner._attribute = resolve
with TemporaryDirectory(prefix="t3t-", dir="/tmp") as directory:
    result = runner.run_case(RunnerRequest(
        case={"id": "task", "user_scenario": {"instructions": "test"}},
        tool_bridge_socket=Path(directory) / "tools.sock",
        base_url="http://unused", app_id="test", agent_id="test", user_id="test",
        user_llm="openai/gpt-4o-mini", timeout_seconds=30,
    ))
assert result.error is None, result.error
assert result.simulation["reply"] == "user reply"
assert len(requests) == 2, len(requests)
assert json.loads(requests[0].content) == json.loads(requests[1].content)
"""
