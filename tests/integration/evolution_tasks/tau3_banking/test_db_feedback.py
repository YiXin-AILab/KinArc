"""显式 opt-in：使用真实 Tau2 ENV 评分器验证 DB 诊断。"""

import os
import subprocess
from pathlib import Path

import pytest


def test_official_db_feedback_preserves_reward_and_replay_count(tmp_path: Path) -> None:
    """遗漏/正确/重复写入均保持官方分数，诊断不增加重放。"""
    dependency = os.environ.get("TEST_TAU2_PROJECT")
    if not dependency:
        pytest.skip("需要显式 TEST_TAU2_PROJECT")
    script = tmp_path / "check_db_feedback.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [str(Path(dependency) / ".venv/bin/python"), str(script)],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).parents[4]),
            "PYTHON_DOTENV_DISABLED": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "3 official DB scenarios passed" in result.stdout


_SCRIPT = '''
from types import SimpleNamespace
from tau2.data_model.message import AssistantMessage, ToolCall
from tau2.data_model.tasks import RewardType
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool
from tau2.evaluator.evaluator_env import EnvironmentEvaluator
from evolution_tasks.tau3_banking._internal.runner import capture_db_checks

class Bank(DB):
    balance: int = 0

class Tools(ToolKitBase):
    @is_tool(ToolType.WRITE)
    def increment(self) -> str:
        """Increment this case balance."""
        self.db.balance += 1
        return str(self.db.balance)

constructed = []
def constructor(**kwargs):
    env = Environment(domain_name="test", policy="test", tools=Tools(Bank()))
    constructed.append(env)
    return env

task = SimpleNamespace(
    initial_state=None,
    evaluation_criteria=SimpleNamespace(
        actions=[SimpleNamespace(
            name="increment", requestor="assistant", arguments={}
        )],
        env_assertions=[], reward_basis=[RewardType.DB],
    ),
)
for writes in (0, 1, 2):
    fixture = constructor()
    messages = []
    for index in range(writes):
        call = ToolCall(
            id=str(index), name="increment", arguments={}, requestor="assistant"
        )
        messages.extend([
            AssistantMessage(role="assistant", tool_calls=[call]),
            fixture.get_response(call),
        ])
    kwargs = dict(
        environment_constructor=constructor, task=task, full_trajectory=messages
    )
    constructed.clear()
    baseline = EnvironmentEvaluator.calculate_reward(**kwargs)
    assert len(constructed) == 2
    constructed.clear()
    with capture_db_checks(EnvironmentEvaluator) as checks:
        observed = EnvironmentEvaluator.calculate_reward(**kwargs)
    assert len(constructed) == 2
    assert observed.model_dump() == baseline.model_dump()
    assert len(checks) == 1
    check = checks[0]
    assert check.db_match == (writes == 1)
    assert check.db_reward == float(writes == 1)
    if writes == 1:
        assert check.differences == []
    else:
        assert len(check.differences) == 1
        diff = check.differences[0]
        assert (diff.database, diff.path, diff.expected, diff.actual) == (
            "agent", "/balance", 1, writes
        )
print("3 official DB scenarios passed")
'''
