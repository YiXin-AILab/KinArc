"""Explicitly enabled online-model smoke through the actual official CLI."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from benchmarks.tau2bench.config import TAU2_PROJECT, load_config

ROOT = Path(__file__).resolve().parents[2]


def test_tau2_native_banking_smoke(tmp_path: Path) -> None:
    """Run two official cases and preserve native scoring, regardless of reward."""
    required = (
        "TAU2_NATIVE_SMOKE_CONFIG",
        "TEST_AGENT_LLM_MODEL",
        "TAU3_USER_LLM",
        "TEST_AGENT_LLM_API_KEY",
        "OPENAI_API_KEY",
    )
    if os.environ.get("TAU2_NATIVE_SMOKE") != "1":
        pytest.skip("TAU2_NATIVE_SMOKE=1 explicitly enables paid model calls")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip("missing explicit smoke inputs: " + ", ".join(missing))
    python = TAU2_PROJECT / ".venv/bin/python"
    if not python.is_file():
        pytest.skip("independent Tau2 environment required")
    integration = load_config(Path(os.environ["TAU2_NATIVE_SMOKE_CONFIG"]))
    config_path = tmp_path / "integration.json"
    config_path.write_text(integration.model_dump_json())
    data = integration.agent_dir / "data"
    before = {p: hashlib.sha256(p.read_bytes()).digest() for p in data.rglob("*.json")}
    output = tmp_path / "official"
    result = subprocess.run(
        [
            str(python),
            "-m",
            "tau2.cli",
            "run",
            "--domain",
            "banking_knowledge",
            "--agent",
            "agentsystem_banking",
            "--agent-llm",
            os.environ["TEST_AGENT_LLM_MODEL"],
            "--user-llm",
            os.environ["TAU3_USER_LLM"],
            "--retrieval-config",
            "bm25_grep",
            "--num-tasks",
            "2",
            "--num-trials",
            "1",
            "--max-concurrency",
            "2",
            "--max-retries",
            "0",
            "--timeout",
            "600",
            "--save-to",
            str(output),
        ],
        cwd=TAU2_PROJECT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHON_DOTENV_DISABLED": "1",
            "TAU2_AGENTSYSTEM_CONFIG": str(config_path),
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        capture_output=True,
        timeout=1500,
    )
    assert result.returncode == 0, "native smoke failed; inspect official results"
    results = json.loads((output / "results.json").read_text())
    assert len(results["simulations"]) == 2
    for simulation in results["simulations"]:
        assert simulation["reward_info"] is not None
        assert any(
            m.get("tool_calls")
            for m in simulation["messages"]
            if m["role"] == "assistant"
        )
    assert not (output / "agentsystem").exists()
    assert all(
        hashlib.sha256(p.read_bytes()).digest() == digest
        for p, digest in before.items()
    )
