"""The portable tasks execute the maintained Runtime without a frozen code fork."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_task_worker_uses_current_python_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution_tasks.evoagentbench_research._internal import process

    (tmp_path / ".local").mkdir()
    captured: dict[str, Any] = {}

    class Completed:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            captured.update(command=command, **kwargs)
            Path(command[command.index("--result") + 1]).write_text('{"ok":true}')

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", Completed)
    assert process.invoke(tmp_path, {"operation": "execute"}, 1) == {"ok": True}
    assert captured["command"][0] == sys.executable
    assert captured["env"]["PYTHONPATH"] == str(ROOT)
    assert "dependencies/runtime-worker" not in json.dumps(captured, default=str)


def test_research_container_mounts_current_public_code_only(tmp_path: Path) -> None:
    from evolution_tasks.evoagentbench_research._internal.research_sandbox import (
        command,
    )

    args = command(
        image="sha256:" + "a" * 64,
        name="eab-retrieval-" + "b" * 32,
        code=ROOT,
        packages=tmp_path / "packages",
        target=tmp_path / "target",
        exchange=tmp_path / "exchange",
        environment={},
    )
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    assert f"type=bind,src={ROOT},dst=/opt/runtime,readonly" not in mounts
    assert any(f"src={ROOT / 'agentsystem'}," in m for m in mounts)
    assert any(f"src={ROOT / 'evolution'}," in m for m in mounts)
    assert not any(
        "/dependencies/runtime-worker" in m or "/dependencies/public-runtime" in m
        for m in mounts
    )
    assert not any(
        f"src={ROOT / 'evolution_tasks/evoagentbench_research'}," in m for m in mounts
    )
    assert not any("/resources" in m or "/.evolution" in m for m in mounts)
    assert "--network=none" in args and "--read-only" in args


def test_legacy_bundles_cannot_restore_a_second_runtime(tmp_path: Path) -> None:
    from evolution_tasks.evoagentbench_research._internal.portable import (
        require_current_project,
    )

    with pytest.raises(ValueError, match="Legacy execution"):
        require_current_project(tmp_path, {"files": []})
    manifest: dict[str, Any] = {"execution_code": "current-project", "files": []}
    require_current_project(tmp_path, manifest)
    (tmp_path / "dependencies/public-runtime").mkdir(parents=True)
    with pytest.raises(ValueError, match="Remove archived"):
        require_current_project(tmp_path, manifest)


def test_current_source_changes_protocol_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.evoagentbench.common import identity

    monkeypatch.setattr(identity, "project_root", lambda: tmp_path)
    core = tmp_path / "agentsystem/engine.py"
    core.parent.mkdir()
    core.write_text("version = 1\n")
    before = identity.source_digest()
    core.write_text("version = 2\n")
    assert identity.source_digest() != before
    before = identity.source_digest()
    adapter = tmp_path / "evolution_tasks/evoagentbench_research/_internal/tool.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("version = 1\n")
    assert identity.source_digest() != before

    before = identity.source_digest()
    (tmp_path / "uv.lock").write_text("version = 1")
    assert identity.source_digest() != before
    before = identity.source_digest()
    (core.parent / "script.lua").write_text("return 1")
    assert identity.source_digest() != before


def test_public_environment_does_not_expose_host_credentials() -> None:
    from evolution_tasks.evoagentbench_research._internal.research_sandbox import (
        public_environment,
    )

    public = public_environment(
        {
            "EAB_MODEL": "openai/test",
            "EAB_API_KEY": "private-execution-key",
            "JUDGE_API_KEY": "private-judge-key",
            "EVOLUTION_LLM_API_KEY": "private-improver-key",
        },
        token="trial-token",
        proxy_url="http://127.0.0.1:19118/v1",
    )
    assert public["EAB_API_KEY"] == "trial-token"
    assert "private-" not in json.dumps(public)


def test_candidate_plugins_require_isolation_and_fixed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentsystem.config.models import AgentConfig
    from evolution_tasks.evoagentbench_research._internal import research_candidate

    contract = {"model": "openai/fixed", "api_key": "trial-token"}
    config = AgentConfig.model_validate(
        {
            "agent_id": "candidate",
            "agent_name": "candidate",
            "prompt": "task",
            "outbound": {"type": "default"},
            "llm_model": contract,
        }
    )
    discovered: list[tuple[object, ...]] = []

    def discover(*args: object) -> tuple[()]:
        discovered.append(args)
        return ()

    monkeypatch.setattr(research_candidate, "discover_application_plugins", discover)
    monkeypatch.delenv("EAB_CANDIDATE_ISOLATED", raising=False)
    with pytest.raises(ValueError, match="isolated"):
        research_candidate.candidate_plugins(tmp_path, config, contract)
    monkeypatch.setenv("EAB_CANDIDATE_ISOLATED", "1")
    with pytest.raises(ValueError, match="model contract"):
        research_candidate.candidate_plugins(
            tmp_path, config, {**contract, "model": "openai/other"}
        )
    assert not discovered
    assert research_candidate.candidate_plugins(tmp_path, config, contract) == ()
    assert len(discovered) == 1


def test_export_excludes_core_but_keeps_data_inside_checkout(tmp_path: Path) -> None:
    import importlib.util

    path = ROOT / "evolution_tasks/evoagentbench_research/scripts/export_resources.py"
    spec = importlib.util.spec_from_file_location("export_current_project", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "agentsystem").mkdir()
    (tmp_path / "evolution").mkdir()
    assert module.project_source(tmp_path)
    assert module.project_source(tmp_path / "agentsystem/config/models.py")
    assert not module.project_source(tmp_path / "resources/hf-cache")


def test_container_install_uses_main_lock_and_matching_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import types

    script = ROOT / "evolution_tasks/evoagentbench_research/scripts/manage.py"
    spec = importlib.util.spec_from_file_location("current_task_manage", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(
        json.dumps(
            {
                "domain": "information_retrieval",
                "research_execution_image": "sha256:test",
            }
        )
    )
    (tmp_path / "configs/upstream-python.txt").write_text("3.12.1")
    (tmp_path / "configs/upstream-requirements.txt").write_text("")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module, "audit", lambda _: None)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *a, **kw: json.dumps(
            [
                "x86_64",
                ["glibc", "2.36"],
                "3.13.15",
            ]
        ),
    )
    commands = []

    def run(command: list[str], **kwargs: Any) -> types.SimpleNamespace:
        commands.append(command)
        if "export" in command:
            assert kwargs["cwd"] == ROOT
            Path(command[-1]).write_text("antlr4-python3-runtime==4.9.3\n")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    module.install(tmp_path)
    assert ["uv", "python", "install", "3.13"] in commands
    target = next(c for c in commands if "--target" in c)
    assert target[target.index("--python") + 1] == "3.13"
    assert "--require-hashes" in target
    assert "--only-binary" not in target
    assert "--python-platform" not in target
    assert not any("runtime-worker" in " ".join(c) for c in commands)
