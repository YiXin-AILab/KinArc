"""领域公开与私有边界，以及评分失败的协议测试。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.domains import domain_worker
from benchmarks.evoagentbench.domains.domain_adapter import DomainEvaluator
from benchmarks.evoagentbench.domains.domain_settings import (
    DomainSettings,
    experiment_identity,
)
from benchmarks.evoagentbench.runners import official_tool_transport
from evolution.evaluator import Evaluator
from evolution.executor import Executor
from evolution.models import (
    CaseExecution,
    ExecutionMetadata,
    ExecutionResult,
)
from evolution.task import load_task, load_task_config
from pydantic import ValidationError


def test_office_public_boundary_does_not_export_grader() -> None:
    result = domain_worker.public_environment(
        "knowledge_work",
        {
            "execution_container": "owned",
            "workspace_dir": "/workspace",
            "rubric": "SECRET",
            "answers": "SECRET",
        },
    )
    assert result == {
        "kind": "knowledge",
        "container": "owned",
        "workspace": "/workspace",
    }


@pytest.mark.parametrize(
    "domain", ["software_engineering", "code_implementation", "unknown"]
)
def test_removed_and_unknown_domains_are_rejected(tmp_path: Path, domain: str) -> None:
    with pytest.raises(ValidationError, match="domain"):
        DomainSettings.model_validate(
            {
                "domain": domain,
                "upstream_repo": tmp_path,
                "upstream_python": tmp_path / "python",
                "domain_config": tmp_path / "domain.yaml",
                "runtime_config": tmp_path / "runtime.yaml",
            }
        )
    with pytest.raises(ValueError, match="Unsupported benchmark domain"):
        domain_worker.load_adapter({"domain": domain})
    with pytest.raises(ValueError, match="Unsupported benchmark domain"):
        domain_worker.public_environment(
            domain, {"execution_container": "owned", "workspace_dir": "/workspace"}
        )
    with pytest.raises(ValueError, match="Unsupported benchmark domain"):
        domain_worker.trial(None, {}, {"settings": {"domain": domain}})


@pytest.mark.parametrize("kind", ["swe", "code", "unknown"])
def test_official_transport_rejects_unsupported_environments(kind: str) -> None:
    environment = {"kind": kind, "container": "owned", "workspace": "/workspace"}
    with pytest.raises(ValueError, match="Unsupported task environment kind"):
        official_tool_transport.execute_tool(environment, "exec", {"command": "pwd"})
    with pytest.raises(ValueError, match="Unsupported task environment kind"):
        official_tool_transport.install_transport(environment)


@pytest.mark.parametrize(
    "error", ["missing_api_key", "judge failed", "grader timed out (10s)"]
)
def test_verifier_infrastructure_error_is_not_zero(error: str) -> None:
    with pytest.raises(RuntimeError):
        domain_worker.normalized_verdict({"reward": 0, "error": error})


def test_office_missing_deliverables_is_a_measured_failure() -> None:
    verdict = domain_worker.normalized_verdict(
        {"reward": 0.0, "error": "no_artifacts_found", "files_found": []}, 0.6
    )
    assert verdict["score"] == 0.0
    assert verdict["passed"] is False


@pytest.mark.parametrize("reward", [True, "1", float("nan"), -1, 2])
def test_reject_invalid_rewards(reward: Any) -> None:
    with pytest.raises(ValueError):
        domain_worker.normalized_verdict({"reward": reward})


def test_worker_lifecycle_keeps_grader_private_and_cleans_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Adapter:
        pass_threshold = staticmethod(lambda: 0.0)

        def initialize(self, args: Any) -> None:
            calls.append("initialize")

        def pre_task_trials(self, task: Any) -> None:
            calls.append("pre")

        def setup(self, *args: Any) -> dict[str, Any]:
            calls.append("setup")
            return {"answer": "PRIVATE"}

        def build_prompt(self, *args: Any) -> str:
            return "public question"

        def verify(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append("verify")
            return {"reward": 0, "error": "missing_api_key"}

        def cleanup(self, *args: Any) -> None:
            calls.append("cleanup")

        def post_task_trials(self, *args: Any) -> None:
            calls.append("post")

        def finalize(self) -> None:
            calls.append("finalize")

    def runtime(*args: Any) -> dict[str, Any]:
        return {"result": "answer", "tracelog": {"status": "succeeded"}}

    monkeypatch.setattr(domain_worker, "run_runtime", runtime)
    with pytest.raises(RuntimeError):
        domain_worker.trial(
            Adapter(),
            {"name": "1"},
            {
                "settings": {"domain": "information_retrieval", "runner": "runtime"},
                "job_tag": "unique",
            },
        )
    assert calls == [
        "initialize",
        "pre",
        "setup",
        "verify",
        "cleanup",
        "post",
        "finalize",
    ]


def test_evaluator_requires_protocol_and_official_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "benchmarks.evoagentbench.domains.domain_settings.dependency_identity",
        lambda settings: "frozen-dependencies",
    )
    config = tmp_path / "config"
    config.write_text("config")
    settings = DomainSettings(
        domain="knowledge_work",
        upstream_repo=tmp_path,
        upstream_python=tmp_path / "python",
        domain_config=config,
        runtime_config=config,
        train_trials=1,
    )
    path = tmp_path / "train.json"
    path.write_text(
        Dataset(
            role="train",
            upstream_revision=settings.upstream_revision,
            source_sha256="abc",
            cases=(Case(id="1", prompt="q"),),
        ).model_dump_json()
    )
    case = CaseExecution(
        id="1",
        result="answer",
        metadata=ExecutionMetadata(total_tokens=4, duration_seconds=1),
        tracelog={
            "status": "succeeded",
            "protocol": experiment_identity(settings, path),
            "verifier": {"score": 0.7, "passed": True, "pass_threshold": 0.6},
        },
    )
    result = DomainEvaluator(settings).evaluate(ExecutionResult(cases=(case,)), path)
    assert result.total_score == 1
    with pytest.raises(ValueError, match="verifier"):
        DomainEvaluator(settings).evaluate(
            ExecutionResult(
                cases=(
                    case.model_copy(
                        update={
                            "tracelog": {
                                "protocol": experiment_identity(settings, path),
                                "status": "succeeded",
                            }
                        }
                    ),
                )
            ),
            path,
        )


@pytest.mark.parametrize(
    "short,domain",
    [
        ("knowledge", "knowledge_work"),
        ("research", "information_retrieval"),
    ],
)
def test_domain_factories_load_through_generic_task_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short: str,
    domain: str,
) -> None:
    monkeypatch.setattr(
        "benchmarks.evoagentbench.domains.domain_settings._image_id",
        lambda image: "sha256:pinned",
    )
    (tmp_path / "target").mkdir()
    (tmp_path / "datasets").mkdir()
    for role in ("train", "validation", "test"):
        (tmp_path / "datasets" / f"{role}.json").write_text("{}")
    for kind in ("executor", "evaluator"):
        (tmp_path / f"{kind}.py").write_text(
            f"from evolution_tasks.evoagentbench_{short}.{kind} import create_{kind}\n"
        )
    (tmp_path / "config.yaml").write_text(
        json.dumps(
            {
                "objective": "Test domain factory",
                "initial_target": "target",
                "workspace": {},
                "settings": {
                    "domain": domain,
                    "upstream_repo": str(tmp_path),
                    "upstream_python": sys.executable,
                    "domain_config": "domain.yaml",
                    "runtime_config": "runtime.yaml",
                },
            }
        )
    )
    bundle = load_task(load_task_config(tmp_path))
    assert isinstance(bundle.executor, Executor)
    assert isinstance(bundle.evaluator, Evaluator)


def test_baseline_uses_new_directory_and_preserves_failed_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "baseline.yaml"
    config.write_text('{"agent":{}}')

    def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert not args[3].exists()
        args[3].mkdir()
        assert kwargs["environment"] == {
            "kind": "research",
            "url": "http://example/mcp",
        }
        return {
            "agent_result": {"completion_status": "timeout", "response": "partial"},
            "usage": {"total_tokens": 2},
        }

    monkeypatch.setattr(
        "benchmarks.evoagentbench.domains.domain_worker.importlib.import_module",
        lambda name: SimpleNamespace(call_nanobot=call),
    )
    result = domain_worker.run_baseline(
        {
            "settings": {
                "official_agent_config": str(config),
                "domain": "information_retrieval",
                "case_timeout_seconds": 10,
            }
        },
        "question",
        {
            "mcp_servers": {
                "bcp-search": {
                    "type": "sse",
                    "url": "http://example/mcp",
                }
            }
        },
        tmp_path,
    )
    assert result["tracelog"]["status"] == "failed"
    assert result["metadata"]["total_tokens"] == 2


def test_office_container_runs_as_workspace_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: commands.append(args))
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    monkeypatch.setattr(os, "getgid", lambda: 1001)
    domain_worker.office_container(
        {"execution_image": "office:test"},
        {"workspace_dir": str(tmp_path)},
        "office-test",
    )
    args = commands[0]
    assert args[args.index("--user") + 1] == "1001:1001"
    assert "HOME=/tmp" in args
    assert "--cap-drop=ALL" in args
