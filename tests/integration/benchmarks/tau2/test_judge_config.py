"""The native NL judge follows the user model without leaking batch settings."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
TAU = ROOT / "benchmarks/tau2bench/tau2-bench"


@pytest.mark.parametrize("user_max_tokens", [None, 2048])
def test_judge_follows_user_and_restores_on_failure(
    tmp_path: Path, user_max_tokens: int | None
) -> None:
    """Exercise the actual batch configuration in the independent Tau2 environment."""
    python = TAU / ".venv/bin/python"
    if not python.exists():
        pytest.skip("independent Tau2 environment required")
    code = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
from benchmarks.tau2bench.config import IntegrationConfig
from benchmarks.tau2bench.lifecycle import batch_scope
from tau2.data_model.simulation import TextRunConfig
from tau2.evaluator import evaluator_nl_assertions as judge

root = Path(sys.argv[1])
source = root / "agent"
source.mkdir()
integration = IntegrationConfig(
    agent_dir=source, runtime_config=root / "runtime.yaml",
    runtime_python=Path(sys.executable),
)
config = TextRunConfig(
    domain="banking_knowledge", retrieval_config="bm25_grep",
    agent="agentsystem_banking", timeout=1200, llm_user="openai/test-shared-judge",
)
original = judge.DEFAULT_LLM_NL_ASSERTIONS
original_judge_args = judge.DEFAULT_LLM_NL_ASSERTIONS_ARGS
if sys.argv[2] != "None":
    config.llm_args_user["max_tokens"] = int(sys.argv[2])
original_user_args = config.llm_args_user
with (
    patch.dict(os.environ, {"TAU2_AGENTSYSTEM_CONFIG":"unused"}),
    patch("benchmarks.tau2bench.lifecycle.load_config", return_value=integration),
    patch("benchmarks.tau2bench.lifecycle.CaseRuntime"),
    patch("benchmarks.tau2bench.lifecycle.worker_command"),
):
    try:
        with batch_scope(config):
            assert judge.DEFAULT_LLM_NL_ASSERTIONS == config.llm_user
            assert config.llm_args_user["max_tokens"] == (
                int(sys.argv[2]) if sys.argv[2] != "None" else 32768
            )
            assert judge.DEFAULT_LLM_NL_ASSERTIONS_ARGS["max_tokens"] == 32768
            raise ValueError("injected batch failure")
    except ValueError as error:
        assert str(error) == "injected batch failure"
assert judge.DEFAULT_LLM_NL_ASSERTIONS == original
assert judge.DEFAULT_LLM_NL_ASSERTIONS_ARGS is original_judge_args
assert config.llm_args_user is original_user_args
"""
    result = subprocess.run(
        [str(python), "-c", code, str(tmp_path), str(user_max_tokens)],
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
