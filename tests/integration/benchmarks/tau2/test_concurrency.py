"""Real Runtime/Redis/UDS integration with a local deterministic model endpoint."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Any

import pytest
import yaml
from redis.asyncio import Redis

ROOT = Path(__file__).resolve().parents[4]
TAU = ROOT / "benchmarks/tau2bench/tau2-bench"


class ModelServer(ThreadingHTTPServer):
    """Respond to actual LiteLLM HTTP requests; assert tool-result visibility."""

    def __init__(self, mode: str) -> None:
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.mode = mode
        self.barrier = Barrier(2)
        self.errors: list[str] = []
        self.labels: set[str] = set()
        self.system_prompts: dict[str, set[str]] = {}
        self.lock = Lock()

    def response(self, request: dict[str, Any]) -> dict[str, Any]:
        """Choose the next action from actual Runtime conversation history."""
        text = json.dumps(request["messages"])
        match = re.search(r"native-case:(case-[ab]) user:([^\s\"\\]+)", text)
        assert match is not None, "missing case identity in model input"
        label, user_id = match.groups()
        if self.mode in {"cli", "golden", "golden_retry"}:
            system = "\n".join(
                message["content"]
                for message in request["messages"]
                if message["role"] == "system"
            )
            assert "<agent_prompt>" not in system
            with self.lock:
                self.system_prompts.setdefault(label, set()).add(system)
        if self.mode in {"golden", "golden_retry"}:
            other = "case-b" if label == "case-a" else "case-a"
            assert f"golden-document:{label}" in system
            assert f"golden-document:{other}" not in system
            assert system.count("<required_documents>") == 1
            tool_names = {tool["function"]["name"] for tool in request["tools"]}
            assert not {"KB_search", "grep"} & tool_names
        tool_messages = [m for m in request["messages"] if m["role"] == "tool"]
        if not tool_messages:
            with self.lock:
                self.labels.add(label)
            self.barrier.wait(timeout=30)
            return self.tool(
                "get_referrals_by_user", {"user_id": user_id}, label + "-read"
            )
        if len(tool_messages) == 1:
            if self.mode == "cutoff":
                # Runtime may observe the closed Bridge before cancellation arrives.
                return {"role": "assistant", "content": "done"}
            content = json.dumps(tool_messages[0]["content"])
            other = "case-b" if label == "case-a" else "case-a"
            assert label in content and other not in content, "case database leaked"
            return self.tool(
                "change_user_email",
                {"user_id": user_id, "new_email": f"{label}@example.invalid"},
                label + "-write",
            )
        return {"role": "assistant", "content": "done"}

    @staticmethod
    def tool(name: str, arguments: dict[str, str], identity: str) -> dict[str, Any]:
        """Return an OpenAI-compatible tool invocation."""
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": identity,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }


class ModelHandler(BaseHTTPRequestHandler):
    """Serve complete and streamed OpenAI chat responses without network models."""

    def do_POST(self) -> None:
        """Capture assertion failures instead of hiding server-thread errors."""
        server = self.server
        assert isinstance(server, ModelServer)
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            message = server.response(request)
            finish = "tool_calls" if message.get("tool_calls") else "stop"
            response: dict[str, Any] = {
                "id": "chatcmpl-native",
                "created": 1,
                "model": "native-test",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
            if request.get("stream"):
                delta = dict(message)
                if delta.get("tool_calls"):
                    delta["tool_calls"] = [
                        dict(c, index=i) for i, c in enumerate(delta["tool_calls"])
                    ]
                response["object"] = "chat.completion.chunk"
                response["choices"] = [
                    {"index": 0, "delta": delta, "finish_reason": finish}
                ]
                usage = response.pop("usage")
                usage_chunk = {**response, "choices": [], "usage": usage}
                body = (
                    "data: "
                    + json.dumps(response)
                    + "\n\ndata: "
                    + json.dumps(usage_chunk)
                    + "\n\ndata: [DONE]\n\n"
                ).encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps(response).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:
            server.errors.append(str(error))
            self.send_error(500, "scripted model assertion failed")

    def log_message(self, format: str, *args: object) -> None:
        """Do not log model request bodies."""


def prepare_target(tmp_path: Path, *, golden: bool = False) -> Path:
    """Use actual application code with a completely explicit test configuration."""
    name = "banking_golden_agent" if golden else "banking_agent"
    target = tmp_path / name
    shutil.copytree(
        ROOT / "application" / name,
        target,
        ignore=shutil.ignore_patterns("config", "__pycache__"),
    )
    (target / "config").mkdir()
    config = {
        "agent_id": "banking-golden-agent" if golden else "banking-agent",
        "agent_name": name,
        "prompt": "prompt/system.md",
        "skill_roots": [],
        "enabled_skills": [],
        "output_mode": "stream",
        "outbound": {"type": "stream"},
        "enabled_tools": ["get_referrals_by_user", "change_user_email"],
        "llm_model": {
            "model": "openai/native-test",
            "api_key": "${TEST_AGENT_LLM_API_KEY}",
            "base_url": "${TEST_AGENT_LLM_BASE_URL}",
            "temperature": 0,
            "max_output_tokens": 256,
            "parallel_tool_calls": False,
            "timeout_seconds": 40,
            "context_window": 32000,
        },
        "compaction": {"enabled": False},
    }
    (target / "config/agent.yaml").write_text(yaml.safe_dump(config))
    return target


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "trials",
        "retry",
        "cleanup_retry",
        "cutoff",
        "cli",
        "golden",
        "golden_retry",
    ],
)
@pytest.mark.asyncio
async def test_real_native_concurrency(tmp_path: Path, mode: str) -> None:
    """Two agents share their own official DB with their user, never each other's."""
    if os.environ.get("TAU2_NATIVE_REDIS") != "1":
        pytest.skip("explicit TAU2_NATIVE_REDIS=1 enables local Redis integration")
    python = TAU / ".venv/bin/python"
    if not python.exists():
        pytest.skip("independent Tau2 installation required")
    target = prepare_target(tmp_path, golden=mode in {"golden", "golden_retry"})
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "redis": {
                    "type": "standalone",
                    "host": os.environ.get("TAU2_NATIVE_REDIS_HOST", "127.0.0.1"),
                    "port": int(os.environ.get("TAU2_NATIVE_REDIS_PORT", "6379")),
                    "password": "${TAU2_NATIVE_REDIS_PASSWORD}",
                },
                "asset": {"max_file_bytes": 1024},
                "asset_store": {"type": "local"},
            }
        )
    )
    config = tmp_path / "integration.json"
    config.write_text(
        json.dumps(
            {
                "agent_dir": str(target),
                "runtime_config": str(runtime),
                "runtime_python": sys.executable,
            }
        )
    )
    server = ModelServer(mode)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [str(python), str(Path(__file__).with_name("native_batch_driver.py"))],
            cwd=TAU,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "PYTHON_DOTENV_DISABLED": "1",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                "TAU2_AGENTSYSTEM_CONFIG": str(config),
                "TAU2_NATIVE_REDIS_PASSWORD": os.environ.get(
                    "TAU2_NATIVE_REDIS_PASSWORD", ""
                ),
                "TEST_AGENT_LLM_API_KEY": "local-test-only",
                "TEST_AGENT_LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "NATIVE_TEST_OUTPUT": str(tmp_path / "official"),
                "NATIVE_TEST_MODE": mode,
            },
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, (server.errors, result.stderr[-6000:])
        assert "NATIVE_CASE_ISOLATION_OK" in result.stdout
        assert server.labels == {"case-a", "case-b"}
        assert not server.errors, server.errors
        line = next(
            s for s in result.stdout.splitlines() if s.startswith("OWNED_PREFIXES=")
        )
        prefixes = json.loads(line.split("=", 1)[1])
        if mode in {"cli", "golden", "golden_retry"}:
            expected_line = next(
                s
                for s in result.stdout.splitlines()
                if s.startswith("EXPECTED_SYSTEM_PROMPTS=")
            )
            expected = json.loads(expected_line.split("=", 1)[1])
            assert server.system_prompts == {
                label: {prompt} for label, prompt in expected.items()
            }
        async with Redis(
            host=os.environ.get("TAU2_NATIVE_REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TAU2_NATIVE_REDIS_PORT", "6379")),
            password=os.environ.get("TAU2_NATIVE_REDIS_PASSWORD") or None,
            socket_timeout=5,
        ) as redis:
            for prefix in prefixes:
                assert [key async for key in redis.scan_iter(match=prefix + ":*")] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
