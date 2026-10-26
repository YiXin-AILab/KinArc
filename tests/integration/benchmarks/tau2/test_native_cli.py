"""Tests execute the actual separately installed tau2 CLI."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
TAU = ROOT / "benchmarks/tau2bench/tau2-bench"


def test_registered_in_native_cli() -> None:
    """Native argparse must expose the custom agent alongside official agents."""
    python = TAU / ".venv/bin/python"
    if not python.exists():
        pytest.skip("install independent Tau2 environment to test native CLI")
    result = subprocess.run(
        [str(python), "-m", "tau2.cli", "run", "--help"],
        cwd=TAU,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHON_DOTENV_DISABLED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "agentsystem_banking" in result.stdout
    assert "llm_agent" in result.stdout


def test_official_initial_greeting_is_accepted() -> None:
    """The official orchestrator supplies its greeting even for fresh cases."""
    python = TAU / ".venv/bin/python"
    if not python.exists():
        pytest.skip("install independent Tau2 environment")
    result = subprocess.run(
        [
            str(python),
            "-c",
            "from benchmarks.tau2bench.adapter import AgentsystemBankingAgent; "
            "from tau2.orchestrator.orchestrator import DEFAULT_FIRST_AGENT_MESSAGE; "
            "agent = object.__new__(AgentsystemBankingAgent); "
            "assert agent.get_init_state([DEFAULT_FIRST_AGENT_MESSAGE]) is None",
        ],
        cwd=TAU,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHON_DOTENV_DISABLED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_adapter_attempts_all_close_stages() -> None:
    """Cancellation or Runtime cleanup errors cannot skip joining the SSE reader."""
    python = TAU / ".venv/bin/python"
    if not python.exists():
        pytest.skip("install independent Tau2 environment")
    program = """
from types import SimpleNamespace
from benchmarks.tau2bench.adapter import AgentsystemBankingAgent
for failure in ("request_stop", "runtime", "session"):
    calls = []
    def close(stage):
        calls.append(stage)
        if stage == failure:
            raise RuntimeError("injected cleanup failure")
    agent = object.__new__(AgentsystemBankingAgent)
    agent.runtime = SimpleNamespace(
        close=lambda: close("runtime"),
        config=SimpleNamespace(shutdown_timeout_seconds=2),
    )
    def close_session(*, timeout):
        assert timeout == 2
        close("session")
    agent.session = SimpleNamespace(
        request_stop=lambda: close("request_stop"), close=close_session
    )
    try:
        agent.stop()
    except ExceptionGroup as error:
        assert len(error.exceptions) == 1
    else:
        raise AssertionError("cleanup failure was hidden")
    assert calls == ["request_stop", "runtime", "session"]
"""
    result = subprocess.run(
        [str(python), "-c", program],
        cwd=TAU,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHON_DOTENV_DISABLED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
