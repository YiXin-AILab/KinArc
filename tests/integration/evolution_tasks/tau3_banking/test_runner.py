"""Tau2 1.0.1 外部环境协议集成测试。"""

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[4]
TASK_DIR = PROJECT_ROOT / "evolution_tasks" / "tau3_banking"
TAU2_PROJECT = TASK_DIR / "tau2-bench"


def test_official_orchestrator_handoff_and_early_stop() -> None:
    """真实官方 Orchestrator/Environment 和 UDS，无模型网络请求。"""
    if not (TAU2_PROJECT / "pyproject.toml").is_file():
        pytest.skip("需要本地 Tau2 dependency")
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(TAU2_PROJECT.resolve()),
            "python",
            "-c",
            _HANDOFF_PROGRAM,
        ],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-5000:]


_HANDOFF_PROGRAM = r'''
import asyncio
import httpx
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tau2.data_model.message import ToolCall, ToolMessage, UserMessage
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool
from tau2.orchestrator.orchestrator import Orchestrator
from evolution_tasks.tau3_banking._internal.adapter import (
    AgentsystemHttpSession, create_http_agent,
)
from evolution_tasks.tau3_banking._internal.tool_bridge import (
    CaseToolBridge, ToolBridgeServer,
)

class Bank(DB):
    balance: int = 0

class Tools(ToolKitBase):
    @is_tool(ToolType.READ)
    def read_balance(self) -> str:
        """Read the current balance."""
        return str(self.db.balance)

    @is_tool(ToolType.WRITE)
    def increment(self) -> str:
        """Increment the balance."""
        assert orchestrator.trajectory[-1].tool_calls[0].name == "increment"
        self.db.balance += 1
        return str(self.db.balance)

class UserTools(ToolKitBase):
    @is_tool(ToolType.WRITE)
    def deposit(self) -> str:
        """Deposit seven units."""
        self.db.balance = 7
        return "deposited"

class User:
    def get_init_state(self, message_history=None):
        return 0
    def generate_next_message(self, message, state):
        if state == 0:
            result = UserMessage(role="user", tool_calls=[
                ToolCall(id="user-deposit", name="deposit",
                    arguments={}, requestor="user")
            ])
        elif state == 1:
            assert isinstance(message, ToolMessage)
            result = UserMessage(role="user", content="Read and increment my balance")
        else:
            result = UserMessage(role="user", content="###STOP###")
        return result, state + 1
    def stop(self, *args):
        pass

class Candidate(AgentsystemHttpSession):
    def submit(self, content):
        # Deterministic HTTP candidate; Tau2 and Runtime have separate dependencies.
        return asyncio.run(self.act())
    async def act(self):
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(socket_path)),
            base_url="http://tau2", timeout=5,
        ) as client:
            for index, name in enumerate(["read_balance"] + ["increment"] * 30):
                response = await client.post("/tools/execute", json={
                    "id": f"call-{index}", "name": name, "arguments": {},
                    "requestor": "assistant",
                })
                response.raise_for_status()
                result = response.json()
                assert not result["error"], result
                if index == 0:
                    assert result["content"] == "7", result
        return "Done"
    def close(self):
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            assert not self._thread.is_alive(), "candidate worker leaked"

scenarios = [(1000, 37, "user_stop"), (5, 7, "max_steps")]
for limit, expected_balance, expected_reason in scenarios:
    with TemporaryDirectory(prefix="t3h-", dir="/tmp") as directory:
        socket_path = Path(directory) / "tools.sock"
        bridge = CaseToolBridge()
        candidate = Candidate(base_url="http://unused", app_id="test", agent_id="bank",
            user_id="user", session_id="case", timeout_seconds=30)
        db = Bank()
        env = Environment(domain_name="test", policy="test", tools=Tools(db),
            user_tools=UserTools(db))
        agent = create_http_agent(tools=env.get_tools(), domain_policy="test",
            session=candidate, bridge=bridge)
        orchestrator = Orchestrator(domain="test", agent=agent, user=User(),
            environment=env, task=SimpleNamespace(id="handoff", initial_state=None),
            max_steps=limit, timeout=20, validate_communication=True)
        with ToolBridgeServer(bridge, socket_path):
            simulation = orchestrator.run()
        assert simulation.termination_reason.value == expected_reason
        assert db.balance == expected_balance, db.balance
        calls = [c for m in simulation.messages
                 for c in (getattr(m, "tool_calls", None) or [])]
        results = [m for m in simulation.messages if isinstance(m, ToolMessage)]
        assert [c.id for c in calls] == [m.id for m in results]
        assert len(calls) == (32 if limit == 1000 else 2)
        if limit == 5:
            # The last official result is recorded but intentionally never delivered.
            assert results[-1].content == "7"
'''
