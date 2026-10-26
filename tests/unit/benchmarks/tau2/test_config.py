"""Native benchmark configuration rejects ambiguous execution inputs."""

import json
from pathlib import Path

import pytest
from benchmarks.tau2bench.config import IntegrationConfig, load_config, validate_run


@pytest.mark.parametrize("timeout", ["12.5", "0", "nan", "invalid"])
def test_environment_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    """Environment values use the same validation and preserve virtualenv paths."""
    monkeypatch.delenv("TAU2_AGENTSYSTEM_CONFIG", raising=False)
    for name in IntegrationConfig.model_fields:
        monkeypatch.delenv(f"TAU2_{name.upper()}", raising=False)
    monkeypatch.setenv("TAU2_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("TAU2_RUNTIME_CONFIG", str(tmp_path / "runtime.yaml"))
    monkeypatch.setenv("TAU2_RUNTIME_PYTHON", str(tmp_path / ".venv/bin/python"))
    monkeypatch.setenv("TAU2_HTTP_TIMEOUT_SECONDS", timeout)
    if timeout != "12.5":
        with pytest.raises(ValueError):
            load_config()
        return
    config = load_config()
    assert config.agent_dir == tmp_path
    assert config.runtime_python == tmp_path / ".venv/bin/python"
    assert config.http_timeout_seconds == 12.5


def test_environment_can_select_existing_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep direct Tau2 callers that select a JSON configuration working."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agent_dir": "agent",
                "runtime_config": "runtime.yaml",
                "runtime_python": "python",
            }
        )
    )
    monkeypatch.setenv("TAU2_AGENTSYSTEM_CONFIG", str(path))
    monkeypatch.setenv("TAU2_AGENT_DIR", "ignored")
    assert load_config().agent_dir == tmp_path / "agent"


def test_load_explicit_paths(tmp_path: Path) -> None:
    """Load a complete configuration without reading deployment files."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agent_dir": str(tmp_path),
                "runtime_config": str(tmp_path / "runtime.yaml"),
                "runtime_python": str(tmp_path / "python"),
            }
        )
    )
    config = load_config(path)
    assert config.agent_dir == tmp_path
    assert config.startup_timeout_seconds == 60
    assert config.shutdown_timeout_seconds == 20
    assert config.http_timeout_seconds == 1200


@pytest.mark.parametrize("relative_config", [False, True])
def test_relative_paths_use_config_directory_without_resolving_python_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_config: bool
) -> None:
    """Changing cwd cannot rebase paths or bypass the virtualenv interpreter."""
    project = tmp_path / "project"
    config_dir = project / "benchmarks/tau2bench"
    config_dir.mkdir(parents=True)
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    interpreter = tmp_path / "system-python"
    interpreter.touch()
    python.symlink_to(interpreter)
    path = config_dir / "local-config.json"
    path.write_text(
        json.dumps(
            {
                "agent_dir": "../../application/banking_agent",
                "runtime_config": "../../config/runtime.yaml",
                "runtime_python": "../../.venv/bin/python",
                "shutdown_timeout_seconds": 20,
            }
        )
    )
    monkeypatch.chdir(tmp_path)
    config = load_config(path.relative_to(tmp_path) if relative_config else path)
    assert config.agent_dir == project / "application/banking_agent"
    assert config.runtime_config == project / "config/runtime.yaml"
    assert config.runtime_python == python
    assert config.runtime_python.is_symlink()
    assert config.shutdown_timeout_seconds == 20


@pytest.mark.parametrize(
    "override",
    [
        {"agent_dir": "relative"},
        {"extra": 1},
        {"http_timeout_seconds": 0},
        {"http_timeout_seconds": True},
        {"http_timeout_seconds": float("inf")},
    ],
)
def test_reject_invalid_configuration(
    tmp_path: Path, override: dict[str, object]
) -> None:
    """Invalid input must fail before resources start."""
    raw = dict(
        agent_dir=str(tmp_path),
        runtime_config=str(tmp_path / "r"),
        runtime_python=str(tmp_path / "p"),
    )
    with pytest.raises(ValueError):
        IntegrationConfig.model_validate(raw | override)


@pytest.mark.parametrize(
    "override",
    [
        {"timeout": None},
        {"timeout": float("inf")},
        {"max_concurrency": 0},
        {"domain": "retail"},
        {"llm_args_agent": {"top_p": 1}},
        {"llm_args_agent": {"temperature": True}},
    ],
)
def test_reject_unsupported_run(override: dict[str, object]) -> None:
    """Native flags cannot silently change the supported evaluation contract."""
    raw: dict[str, object] = dict(
        domain="banking_knowledge",
        retrieval_config="bm25_grep",
        timeout=30,
        max_concurrency=2,
        llm_agent="openai/test",
        llm_args_agent={},
    )
    with pytest.raises(ValueError):
        validate_run(raw | override)


@pytest.mark.parametrize("variant", ["bm25", "full_kb", "golden_retrieval", "alltools"])
def test_native_retrieval_configuration_is_forwarded(variant: str) -> None:
    """Retrieval selection belongs to Tau2 and the user's Agent configuration."""
    validate_run(
        {
            "domain": "banking_knowledge",
            "retrieval_config": variant,
            "retrieval_config_kwargs": {"top_k": 3},
            "timeout": 30,
            "max_concurrency": 2,
            "llm_agent": "openai/test",
            "llm_args_agent": {},
        }
    )
