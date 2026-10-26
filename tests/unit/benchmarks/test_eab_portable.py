"""Portable task imports preserve immutable history and reject missing resources."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import pytest

ROOT = Path(__file__).resolve().parents[3]


def support(domain: str = "research") -> ModuleType:
    path = ROOT / f"evolution_tasks/evoagentbench_{domain}/_internal/portable.py"
    spec = importlib.util.spec_from_file_location("eab_portable_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_relocation_checks_content_and_rejects_escape(tmp_path: Path) -> None:
    module = support()
    (tmp_path / "data").mkdir()
    file = tmp_path / "data/item"
    file.write_bytes(b"frozen")
    row = {
        "path": "data/item",
        "sha256": hashlib.sha256(b"frozen").hexdigest(),
        "bytes": 6,
    }
    module.verify_files(tmp_path, [row])
    with pytest.raises(ValueError, match="outside"):
        module.local_path(tmp_path, "../elsewhere")
    file.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash|changed"):
        module.verify_files(tmp_path, [row])


@pytest.mark.parametrize("domain", ["knowledge", "research"])
def test_resource_check_accepts_rebuilt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    """Rebuilt resources need not match historical bytes; history still must."""
    module = support(domain)
    resource = tmp_path / "corpus.sqlite"
    resource.write_bytes(b"rebuilt corpus")
    row = {"path": resource.name, "bytes": 1, "sha256": "0" * 64}

    def forbidden(*args: Any) -> NoReturn:
        raise AssertionError("Resource presence check must not hash the file")

    with monkeypatch.context() as patch:
        patch.setattr(module, "sha256", forbidden)
        module.verify_files(tmp_path, [row], check_hashes=False)
    with pytest.raises(ValueError, match="hash changed"):
        module.verify_files(tmp_path, [row])
    resource.unlink()
    with pytest.raises(FileNotFoundError, match="corpus.sqlite"):
        module.verify_files(tmp_path, [row], check_hashes=False)
    with pytest.raises(ValueError, match="outside"):
        module.verify_files(
            tmp_path, [{**row, "path": "../outside"}], check_hashes=False
        )


@pytest.mark.parametrize("domain", ["knowledge", "research"])
def test_prepare_freezes_rebuilt_resources_for_current_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    """Freeze current resources without rewriting the historical inventory."""
    import sys

    from benchmarks.evoagentbench.domains.domain_dependencies import validate

    monkeypatch.setitem(sys.modules, "portable", support(domain))
    environment = ModuleType("environment")
    monkeypatch.setattr(
        environment, "verify_environment", lambda root: None, raising=False
    )
    monkeypatch.setitem(sys.modules, "environment", environment)
    path = ROOT / f"evolution_tasks/evoagentbench_{domain}/_internal/prepare.py"
    spec = importlib.util.spec_from_file_location(f"eab_prepare_{domain}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "source_digest", lambda: "fixture-project")

    resource = tmp_path / "corpus.sqlite"
    resource.write_bytes(b"rebuilt corpus")
    manifest = tmp_path / "manifests/resources.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "execution_code": "current-project",
                "files": [{"path": resource.name, "bytes": 1, "sha256": "0" * 64}],
            }
        )
    )
    original_manifest = manifest.read_bytes()
    configs = tmp_path / "configs"
    configs.mkdir()
    python = tmp_path / "python"
    python.touch()
    (configs / "settings.json").write_text(
        json.dumps(
            {
                "domain": "knowledge_work",
                "upstream_python": python.name,
            }
        )
    )
    (configs / "domain.yaml").write_text("{}")
    (configs / "runtime.yaml").write_text("{}")

    settings = module.prepare(tmp_path)
    current = validate(Path(settings["dependency_manifest"]))
    assert current["files"][0]["bytes"] == len(b"rebuilt corpus")
    assert (
        current["files"][0]["sha256"] == hashlib.sha256(b"rebuilt corpus").hexdigest()
    )
    relocation = tmp_path / ".local/relocation.json"
    first_identity = json.loads(relocation.read_text())["protocol_id"]
    resource.write_bytes(b"a different rebuilt corpus")
    with pytest.raises(ValueError, match="dependency file changed"):
        validate(Path(settings["dependency_manifest"]))
    settings = module.prepare(tmp_path)
    validate(Path(settings["dependency_manifest"]))
    assert json.loads(relocation.read_text())["protocol_id"] != first_identity
    assert manifest.read_bytes() == original_manifest


def test_result_conversion_preserves_original_and_rollback(tmp_path: Path) -> None:
    module = support()
    source = tmp_path / "source"
    source.mkdir()
    candidate = source / "candidate-20261009T120037"
    candidate.mkdir()
    evaluation = candidate / "train/evaluation.json"
    evaluation.parent.mkdir()
    evaluation.write_text(json.dumps({"challenge_comparison": {"parent_id": "p"}}))
    rollback = candidate / "rollback.json"
    rollback.write_text(
        json.dumps(
            {
                "source_result_hashes": {
                    "train": {
                        "evaluation_sha256": hashlib.sha256(
                            evaluation.read_bytes()
                        ).hexdigest(),
                        "execution_sha256": "e" * 64,
                    }
                }
            }
        )
    )
    original = evaluation.read_bytes()
    original_rollback = rollback.read_bytes()
    destination = tmp_path / "new"
    manifest = module.migrate_candidates(source, destination)
    assert evaluation.read_bytes() == original
    assert rollback.read_bytes() == original_rollback
    copied = destination / candidate.name / "train/evaluation.json"
    assert "parent_comparison" in json.loads(copied.read_text())
    assert (
        destination / candidate.name / "rollback.json"
    ).read_bytes() == original_rollback
    assert manifest["candidate_count"] == 1
    assert (
        manifest["changed"][0]["source_sha256"] == hashlib.sha256(original).hexdigest()
    )


def test_migration_rejects_existing_destination(tmp_path: Path) -> None:
    module = support()
    with pytest.raises(FileExistsError):
        module.migrate_candidates(tmp_path, tmp_path)


def test_config_relocation_does_not_replace_prompt_text(tmp_path: Path) -> None:
    module = support()
    mapping = {"/old/corpus": "resources/corpus"}
    result = module.relocate(
        {
            "dataset_file": "/old/corpus/questions.jsonl",
            "prompt": "Please look at /old/corpus",
        },
        tmp_path,
        mapping,
    )
    assert result["dataset_file"] == str(tmp_path / "resources/corpus/questions.jsonl")
    assert result["prompt"] == "Please look at /old/corpus"
    with pytest.raises(ValueError, match="Unmapped"):
        module.relocate({"dataset_file": "/unknown/data"}, tmp_path, mapping)


def test_empty_test_placeholder_is_not_executable(tmp_path: Path) -> None:
    module = support()
    path = tmp_path / "test.json"
    path.write_text('{"cases":[]}')
    with pytest.raises(ValueError, match="empty"):
        module.check_dataset(path)


def recovery_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    import sys

    monkeypatch.setitem(sys.modules, "portable", support())
    path = ROOT / "evolution_tasks/evoagentbench_research/_internal/recovery.py"
    spec = importlib.util.spec_from_file_location("eab_recovery_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recovery_regrades_complete_output_without_agent_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from threading import Event

    module = recovery_module(monkeypatch)
    directory = tmp_path / ".local/deadlines/case"
    directory.mkdir(parents=True)
    (directory / "pending.json").write_text('{"job_tag":"old"}')
    output = tmp_path / ".local/evidence/outputs/old/execution.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps({"result": "answer", "tracelog": {"status": "succeeded"}})
    )
    calls = []

    def invoke(
        command: list[str], request: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        calls.append((command, request))
        return {"result": "answer", "tracelog": {"verifier": {"score": 0}}}

    result = module.recover(
        tmp_path,
        directory,
        invoke,
        ["python", "domain_worker.py"],
        {"settings": {"domain": "information_retrieval"}, "ids": ["case"]},
        {},
        deadline=time.time() + 1200,
        stop=Event(),
        exhausted=lambda: {"zero": True},
    )
    assert result["tracelog"]["verifier"]["score"] == 0
    assert len(calls) == 1
    assert calls[0][0][1].endswith("/regrade.py")
    assert calls[0][1]["execution"] == str(output)
    assert output.read_text() == json.dumps(
        {"result": "answer", "tracelog": {"status": "succeeded"}}
    )


def test_recovery_reuses_valid_low_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from threading import Event

    module = recovery_module(monkeypatch)
    directory = tmp_path / "ledger"
    directory.mkdir()
    (directory / "pending.json").write_text('{"job_tag":"old"}')
    output = tmp_path / ".local/evidence/outputs/old/execution.json"
    output.parent.mkdir(parents=True)
    value = {
        "result": "answer",
        "tracelog": {"status": "succeeded", "verifier": {"score": 0}},
    }
    output.write_text(json.dumps(value))

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("A valid low score must not be resampled")

    assert (
        module.recover(
            tmp_path,
            directory,
            forbidden,
            [],
            {"settings": {"domain": "information_retrieval"}},
            {},
            deadline=time.time() + 1200,
            stop=Event(),
            exhausted=forbidden,
        )
        == value
    )


def test_recovery_deadline_and_cancellation_never_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import time
    from threading import Event

    module = recovery_module(monkeypatch)

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Must not dispatch")

    stop = Event()
    assert module.recover(
        tmp_path,
        tmp_path,
        forbidden,
        [],
        {},
        {},
        deadline=time.time() - 1,
        stop=stop,
        exhausted=lambda: {"zero": True},
    ) == {"zero": True}
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        module.recover(
            tmp_path,
            tmp_path,
            forbidden,
            [],
            {},
            {},
            deadline=time.time() - 1,
            stop=stop,
            exhausted=forbidden,
        )


def test_migration_preserves_changelog_without_archives(tmp_path: Path) -> None:
    module = support()
    source = tmp_path / "source"
    for index in range(22):
        candidate = source / f"candidate-20261009T12{index:02}00"
        candidate.mkdir(parents=True)
        (candidate / "changelog.md").write_text(f"original note {index}")
    before = module.file_inventory(source)
    destination = tmp_path / ".evolution/candidates"
    manifest = module.migrate_candidates(source, destination)
    assert manifest["candidate_count"] == 20
    assert (
        sorted(p.name for p in destination.iterdir())
        == sorted(p.name for p in source.iterdir())[:20]
    )
    assert module.file_inventory(source) == before
    assert manifest["changed"] == []
    assert not (destination.parent / "historical-originals").exists()
    for row in manifest["files"]:
        assert row["source_sha256"] == row["sha256"]
        assert (destination / row["path"]).read_bytes() == (
            source / row["path"]
        ).read_bytes()


def test_vcs_wheel_keeps_source_license_and_drops_host_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile

    path = ROOT / "evolution_tasks/evoagentbench_research/_internal/wheels.py"
    spec = importlib.util.spec_from_file_location("eab_wheels_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    content = {
        "sample/__init__.py": b"VALUE = 42\n",
        "sample-0.1.dist-info/METADATA": b"Name: sample\nVersion: 0.1\n",
        "sample-0.1.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "sample-0.1.dist-info/licenses/LICENSE": b"original license",
        "sample-0.1.dist-info/direct_url.json": b'{"url":"private-host"}',
    }
    for name, value in content.items():
        file = tmp_path / "installed" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value)

    class Distribution:
        metadata = {"Name": "sample"}
        version = "0.1"
        files = list(content)

        def read_text(self, name: str) -> str:
            if name == "direct_url.json":
                return '{"vcs_info":{"commit_id":"abc"}}'
            return content["sample-0.1.dist-info/" + name].decode()

        def locate_file(self, name: str) -> Path:
            return tmp_path / "installed" / name

    monkeypatch.setattr(
        module.importlib.metadata, "distributions", lambda: [Distribution()]
    )
    manifest = module.export_vcs_wheels(tmp_path / "wheels")
    assert manifest[0]["vcs_commit"] == "abc"
    with zipfile.ZipFile(tmp_path / "wheels" / manifest[0]["wheel"]) as archive:
        assert archive.read("sample/__init__.py") == content["sample/__init__.py"]
        assert (
            archive.read("sample-0.1.dist-info/licenses/LICENSE") == b"original license"
        )
        assert not any("direct_url" in name for name in archive.namelist())
        assert b"sample/__init__.py,sha256=" in archive.read(
            "sample-0.1.dist-info/RECORD"
        )


def test_worker_descendants_use_current_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ROOT / "evolution_tasks/evoagentbench_research/_internal/process.py"
    spec = importlib.util.spec_from_file_location("eab_process_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("PYTHONPATH", "/unrelated/current/project")
    calls = []

    class Process:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            assert command[2] == str(path.with_name("runner.py"))
            assert command[command.index("--task-dir") + 1] == str(tmp_path)
            calls.append(kwargs)
            result = Path(command[command.index("--result") + 1])
            result.write_text('{"ok":true}')

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    assert module.invoke(tmp_path, {"operation": "execute"}, 30) == {"ok": True}
    assert calls[0]["env"]["PYTHONPATH"] == str(ROOT)


def test_old_rollback_links_digest_without_original_bytes(tmp_path: Path) -> None:
    module = support()
    source = tmp_path / ".evolution/candidates/source/train/evaluation.json"
    source.parent.mkdir(parents=True)
    source.write_text("migrated")
    digest = module.sha256(source)
    row = {
        "path": "source/train/evaluation.json",
        "bytes": 8,
        "sha256": digest,
        "source_sha256": "a" * 64,
    }
    migration = {"files": [{"path": "old/rollback.json"}, row]}
    assert module.rollback_hash(tmp_path, migration, "old", source) == "a" * 64
    assert module.rollback_hash(tmp_path, migration, "new", source) == digest
    source.write_text("corrupted")
    with pytest.raises(ValueError, match="hash changed"):
        module.rollback_hash(tmp_path, migration, "old", source)


def test_recovery_retains_verdict_after_cleanup_uses_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    module = recovery_module(monkeypatch)
    directory = tmp_path / "ledger"
    directory.mkdir()
    value = {
        "result": "answer",
        "tracelog": {"status": "succeeded", "verifier": {"score": 0.5}},
    }
    clock = [100.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])

    def invoke(
        command: list[str], request: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        output = (
            tmp_path / ".local/evidence/outputs" / request["job_tag"] / "execution.json"
        )
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps(value))
        clock[0] = 201.0
        raise TimeoutError("cleanup stalled after valid grading")

    assert (
        module.recover(
            tmp_path,
            directory,
            invoke,
            ["python"],
            {"settings": {"domain": "information_retrieval"}, "ids": ["case"]},
            {},
            deadline=200.0,
            stop=Event(),
            exhausted=lambda: {"zero": True},
        )
        == value
    )


def test_regrade_uses_current_verification_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    monkeypatch.setitem(sys.modules, "portable", support())
    path = ROOT / "evolution_tasks/evoagentbench_research/_internal/regrade.py"
    spec = importlib.util.spec_from_file_location("eab_regrade_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worker: Any = ModuleType("benchmarks.evoagentbench.domains.domain_worker")
    adapter = SimpleNamespace(load_tasks=lambda _: [{}], pass_threshold=lambda: 0)
    worker.load_adapter = lambda _: adapter
    worker.normalized_verdict = lambda *args: {"score": 1}
    calls = []

    def guard(
        adapter: Any,
        task: Any,
        env: Any,
        directory: Any,
        request: dict[str, Any],
        response: Any,
    ) -> NoReturn:
        calls.append((request["job_tag"], response))
        raise RuntimeError("Retrieval judge failed")

    worker.verify_response = guard
    monkeypatch.setitem(sys.modules, worker.__name__, worker)
    execution = tmp_path / "execution.json"
    execution.write_text(
        json.dumps({"result": "complete answer", "tracelog": {"status": "succeeded"}})
    )
    request = {
        "settings": {"domain": "information_retrieval"},
        "execution": str(execution),
        "case_id": "case",
        "job_tag": "original",
        "output": str(tmp_path / "grade"),
    }
    with pytest.raises(RuntimeError, match="Retrieval judge failed"):
        module.grade(request)
    assert calls == [("original", "complete answer")]


def test_current_worker_checkpoints_before_entering_grader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from benchmarks.evoagentbench.domains import domain_worker as worker

    archived: list[dict[str, Any]] = []

    def noop(*args: Any) -> None:
        pass

    adapter = SimpleNamespace(
        initialize=noop,
        finalize=noop,
        pre_task_trials=noop,
        post_task_trials=noop,
        setup=lambda *args: {},
        cleanup=noop,
        build_prompt=lambda *args: "prompt",
    )
    monkeypatch.setattr(worker, "preflight_task", noop)
    monkeypatch.setattr(
        worker,
        "run_runtime",
        lambda *args: {
            "result": "complete answer",
            "tracelog": {"status": "succeeded", "verifier": {"score": 1}},
        },
    )
    monkeypatch.setattr(
        worker, "archive_outputs", lambda *args: archived.append(args[-1])
    )

    def interrupted(*args: Any) -> NoReturn:
        assert archived[0]["result"] == "complete answer"
        assert "verifier" not in archived[0]["tracelog"]
        raise RuntimeError("grader interruption")

    monkeypatch.setattr(worker, "verify_response", interrupted)
    with pytest.raises(RuntimeError, match="grader interruption"):
        worker.trial(
            adapter,
            {"name": "case"},
            {
                "settings": {"domain": "information_retrieval", "runner": "runtime"},
                "job_tag": "attempt",
            },
        )
    assert len(archived) == 2


def test_actual_recovery_scope_is_revoked_and_usage_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import ModuleType

    module = recovery_module(monkeypatch)
    proxy: Any = ModuleType(
        "evolution_tasks.evoagentbench_research._internal.research_model_proxy"
    )
    redis: Any = ModuleType(
        "evolution_tasks.evoagentbench_research._internal.research_redis"
    )
    sandbox: Any = ModuleType(
        "evolution_tasks.evoagentbench_research._internal.research_sandbox"
    )
    revoked = []
    proxy.revoke_scope = lambda path, tag: revoked.append(("model", tag))
    redis.revoke_redis_scope = lambda tag: revoked.append(("redis", tag))
    proxy.read_scope_usage = lambda path, tag: {
        "total_tokens": 99,
        "pending_requests": 0,
    }

    def account(value: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
        value["metadata"]["total_tokens"] = max(
            value["metadata"]["total_tokens"], usage["total_tokens"]
        )
        value["tracelog"]["model_proxy_usage"] = usage
        return value

    sandbox.account_usage = account
    for fake in (proxy, redis, sandbox):
        monkeypatch.setitem(sys.modules, fake.__name__, fake)
    result = module.close_scope(
        {"research_proxy_scopes": str(tmp_path)},
        "actual-attempt",
        {"metadata": {"total_tokens": 50}, "tracelog": {}},
    )
    assert revoked == [("model", "actual-attempt"), ("redis", "actual-attempt")]
    assert result["metadata"]["total_tokens"] == 99
    assert result["tracelog"]["model_proxy_usage"]["total_tokens"] == 99


def test_assemble_rejects_incomplete_history_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "portable", support())
    path = ROOT / "evolution_tasks/evoagentbench_research/scripts/manage.py"
    spec = importlib.util.spec_from_file_location("eab_manage_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    resources = tmp_path / "resources"
    (resources / "manifests").mkdir(parents=True)
    (resources / "manifests/resources.json").write_text('{"files":[]}')
    destination = tmp_path / "task"
    with pytest.raises(FileNotFoundError):
        module.assemble(source, resources, destination)
    assert not destination.exists()


def test_completed_regrade_is_reused_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    module = recovery_module(monkeypatch)
    directory = tmp_path / "ledger"
    directory.mkdir()
    (directory / "pending.json").write_text('{"job_tag":"old"}')
    output = tmp_path / ".local/evidence/outputs/old/execution.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps({"result": "answer", "tracelog": {"status": "succeeded"}})
    )
    value = {
        "result": "answer",
        "tracelog": {"status": "succeeded", "verifier": {"score": 0}},
    }
    clock = [100.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])

    def invoke(
        command: list[str], request: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert command[1].endswith("regrade.py")
        (Path(request["output"]) / "result.json").write_text(json.dumps(value))
        clock[0] = 201.0
        raise TimeoutError("grader completed before process cleanup")

    assert (
        module.recover(
            tmp_path,
            directory,
            invoke,
            ["python"],
            {"settings": {"domain": "information_retrieval"}, "ids": ["case"]},
            {},
            deadline=200.0,
            stop=Event(),
            exhausted=lambda: {"zero": True},
        )
        == value
    )


def test_resume_entrypoint_really_invokes_current_cli() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "from evolution.cli import main; main()", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "{run,test,status}" in result.stdout


def test_resume_config_validates_and_owns_task_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import SimpleNamespace

    import yaml
    from agentsystem.config.loader import load_yaml_config
    from agentsystem.config.models import LocalAssetStoreConfig, RuntimeConfig

    monkeypatch.setitem(sys.modules, "portable", support())
    path = ROOT / "evolution_tasks/evoagentbench_research/scripts/manage.py"
    spec = importlib.util.spec_from_file_location("eab_resume_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "audit", lambda _: None)
    monkeypatch.setattr(module, "validate_history", lambda _: None)
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local/services.json").write_text('{"status":"ready"}')
    (tmp_path / "config.yaml").write_text("max_iterations: 0\n")
    env = {
        "EAB_REDIS_HOST": "localhost",
        "EAB_REDIS_PORT": "6379",
        "EAB_REDIS_USERNAME": "default",
        "EAB_REDIS_PASSWORD": "test-only",
    }
    calls = []

    async def fake_run(root: Path, runtime: Path, iterations: int) -> None:
        calls.append((root, runtime, iterations))

    monkeypatch.setattr(module, "run_iterations", fake_run)
    module.resume(
        SimpleNamespace(task_dir=tmp_path, additional_iterations=1, runtime_config=None)
    )
    config = load_yaml_config(
        tmp_path / ".local/improver-runtime.yaml", RuntimeConfig, environ=env
    )
    assert isinstance(config.asset_store, LocalAssetStoreConfig)
    assert config.asset_store.root == Path(".local/improver-assets")
    assert config.logging.directory == Path(".local/improver-logs")
    assert calls == [(tmp_path, tmp_path / ".local/improver-runtime.yaml", 1)]
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["max_iterations"] == 0


def test_two_task_support_copies_stay_consistent() -> None:
    office = ROOT / "evolution_tasks/evoagentbench_knowledge"
    research = ROOT / "evolution_tasks/evoagentbench_research"
    for directory in ("_internal", "scripts"):
        a = {p.name: p.read_bytes() for p in (office / directory).glob("*.py")}
        b = {p.name: p.read_bytes() for p in (research / directory).glob("*.py")}
        assert a == {
            name: content
            for name, content in b.items()
            if not name.startswith("research_")
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["train", "validation"])
async def test_portable_evaluator_matches_current_engine_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    from evolution.engine import EvolutionEngine
    from evolution.models import DatasetRole, ExecutionResult
    from evolution.task import TaskBundle

    path = ROOT / "evolution_tasks/evoagentbench_research/_internal/loading.py"
    spec = importlib.util.spec_from_file_location("eab_current_port_test", path)
    assert spec is not None and spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    module = loader.adapter(path.parent.parent / "evaluator.py")
    dataset = tmp_path / "subset.json"
    dataset.write_text(json.dumps({"role": role, "cases": [{"id": "one"}]}))
    calls = []

    def invoke(root: Path, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(request)
        return {"total_score": 0.0, "cases": []}

    monkeypatch.setattr(module, "invoke", invoke)
    # Exercise the real method with only its required protocol collaborators.
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine._task = TaskBundle(
        task_dir=tmp_path,
        executor=module.Executor(tmp_path, {"portable": True}),
        evaluator=module.Evaluator(tmp_path, {"portable": True}),
        datasets={DatasetRole(role): dataset},
        digest="test-only",
    )
    result = await EvolutionEngine._evaluate(
        engine, ExecutionResult(cases=()), DatasetRole(role)
    )
    assert result.total_score == 0
    assert calls[0]["operation"] == "evaluate"
    assert calls[0]["role"] == role


@pytest.mark.parametrize(
    ("candidate_id", "has_execution", "has_evaluation", "allowed"),
    [
        ("old", True, False, False),
        ("new", True, False, True),
        ("new", False, True, False),
    ],
)
def test_history_validation_allows_only_new_pending_aggregation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_id: str,
    has_execution: bool,
    has_evaluation: bool,
    allowed: bool,
) -> None:
    import sys
    from types import SimpleNamespace

    import evolution.candidates

    monkeypatch.setitem(sys.modules, "portable", support())
    path = ROOT / "evolution_tasks/evoagentbench_research/scripts/manage.py"
    spec = importlib.util.spec_from_file_location("eab_partial_history_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "config.yaml").write_text("objective: test\ninitial_target: .\n")
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests/history-migration.json").write_text(
        json.dumps({"files": [{"path": "old/target/agent.yaml"}]})
    )
    monkeypatch.setattr(module, "validate_problem_library", lambda *_: None)
    repository = SimpleNamespace(
        list_candidates=lambda: [SimpleNamespace(id=candidate_id, parent_id=None)],
        load_execution=lambda *_: object() if has_execution else None,
        load_evaluation=lambda *_: object() if has_evaluation else None,
        load_rollback=lambda *_: None,
    )
    monkeypatch.setattr(
        evolution.candidates, "CandidateRepository", lambda *args, **kwargs: repository
    )
    if allowed:
        assert module.validate_history(tmp_path)["pending_evaluations"] == 2
    else:
        with pytest.raises(ValueError, match="Partial historical|without execution"):
            module.validate_history(tmp_path)


def test_current_migration_is_idempotent_and_detects_tampering(tmp_path: Path) -> None:
    module = support()
    candidate = tmp_path / "candidate-20261009T120037/train"
    candidate.mkdir(parents=True)
    path = candidate / "evaluation.json"
    path.write_text('{"parent_comparison": null}')
    before = path.read_bytes()
    ledger = module.history_inventory(tmp_path, {})
    assert module.history_inventory(tmp_path, ledger) == ledger
    assert path.read_bytes() == before
    path.write_text('{"parent_comparison": {}}')
    with pytest.raises(ValueError, match="Historical file changed"):
        module.history_inventory(tmp_path, ledger)


def test_missing_resources_reports_all_paths(tmp_path: Path) -> None:
    rows = [{"path": name, "bytes": 1, "sha256": "0" * 64} for name in ["a", "b"]]
    with pytest.raises(FileNotFoundError) as caught:
        support().verify_files(tmp_path, rows)
    assert str(caught.value) == "Missing resources:\na\nb"


def test_zero_budget_resume_never_checks_services_or_calls_models(
    tmp_path: Path,
) -> None:
    from argparse import Namespace
    from unittest.mock import patch

    from evolution_tasks.evoagentbench_research.scripts import manage

    with (
        patch.object(manage, "restore_check") as restore,
        patch.object(manage, "audit") as audit,
    ):
        manage.resume(Namespace(task_dir=tmp_path, additional_iterations=0))
    restore.assert_called_once_with(tmp_path)
    audit.assert_not_called()


@pytest.mark.parametrize("domain", ["knowledge", "research"])
@pytest.mark.parametrize("legacy_initial", [False, True])
def test_assembly_keeps_twenty_and_references_public_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str, legacy_initial: bool
) -> None:
    import yaml
    from evolution.candidates import tree_digest
    from evolution.task import load_task_config

    manage = importlib.import_module(
        f"evolution_tasks.evoagentbench_{domain}.scripts.manage"
    )

    template = tmp_path / "template"
    template.mkdir()
    for name in ("executor.py", "evaluator.py"):
        (template / name).write_text("# shared implementation\n")
    monkeypatch.setattr(manage, "TEMPLATE", template)
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text(
        "objective: fixture\nmax_candidates: 99\nworkspace: {read_only: []}\n"
    )
    if legacy_initial:
        (source / "initial_target").mkdir()
        (source / "initial_target/prompt.md").write_text("outdated duplicate")
    (source / "datasets").mkdir()
    for role in ("train", "validation", "test"):
        (source / "datasets" / f"{role}.json").write_text(
            json.dumps({"role": role, "cases": [{"id": role, "prompt": "fixture"}]})
        )
    previous = ""
    for index in range(22):
        candidate_id = f"candidate-20261009T12{index:02}00"
        candidate = source / ".evolution/candidates" / candidate_id
        (candidate / "target").mkdir(parents=True)
        (candidate / "target/prompt.md").write_text(str(index))
        (candidate / "target.sha256").write_text(tree_digest(candidate / "target"))
        (candidate / "parent").write_text(previous)
        (candidate / "changelog.md").write_text("formal note")
        previous = candidate_id
    seed_id = "candidate-20261009T120000"
    (source / ".evolution/manifest.json").write_text(
        json.dumps({"seed_candidate_id": seed_id})
    )
    (source / "history").mkdir()
    (source / "history/config.original.yaml").write_text("old")
    (source / ".evolution/historical-originals").mkdir()
    (source / ".evolution/historical-originals/raw.json").write_text("old")
    resources = tmp_path / "bundle"
    for directory in ("resources", "dependencies", "configs", "manifests"):
        (resources / directory).mkdir(parents=True)
    (resources / "manifests/resources.json").write_text(
        json.dumps({"execution_code": "current-project", "files": []})
    )
    before = support().file_inventory(source)
    destination = tmp_path / "derived"
    manage.assemble(source, resources, destination)
    assert support().file_inventory(source) == before
    assert not list(destination.glob("*.py"))
    assert not (destination / "_internal").exists()
    assert not (destination / "history").exists()
    assert not (destination / ".evolution/historical-originals").exists()
    assert not (destination / ".local").exists()
    assert not (destination / "initial_target").exists()
    assert len(list((destination / ".evolution/candidates").iterdir())) == 20
    config = yaml.safe_load((destination / "config.yaml").read_text())
    assert config["max_iterations"] == 0 and "max_candidates" not in config
    assert config["executor"] == "../template/executor.py"
    assert config["initial_target"] == f".evolution/candidates/{seed_id}/target"
    assert load_task_config(destination).executor == template / "executor.py"
    assert manage.verify_history_files(destination)["candidate_count"] == 20

    # A different manifest seed must not silently rebase the imported history.
    (source / ".evolution/manifest.json").write_text(
        json.dumps({"seed_candidate_id": "candidate-20261009T120100"})
    )
    invalid = tmp_path / "invalid"
    with pytest.raises(ValueError, match="earliest retained"):
        manage.assemble(source, resources, invalid)
    assert not invalid.exists()


@pytest.mark.parametrize("domain", ["knowledge", "research"])
def test_smoke_uses_configured_seed_without_duplicate_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    from types import SimpleNamespace

    import evolution.task
    from evolution.models import EvaluationResult, ExecutionResult

    manage = importlib.import_module(
        f"evolution_tasks.evoagentbench_{domain}.scripts.manage"
    )
    loading = importlib.import_module(
        f"evolution_tasks.evoagentbench_{domain}._internal.loading"
    )
    adapter = loading.adapter(manage.TEMPLATE / "executor.py")
    seed = tmp_path / ".evolution/candidates/candidate-20261009T120000/target"
    seed.mkdir(parents=True)
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local/services.json").write_text('{"status":"ready"}')
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets/train.json").write_text(
        '{"role":"train","cases":[{"id":"one","prompt":"fixture"}]}'
    )
    monkeypatch.setattr(
        evolution.task,
        "load_task_config",
        lambda _: SimpleNamespace(initial_target=seed),
    )
    targets = []

    def execute(self: Any, target: Path, *args: Any, **kwargs: Any) -> ExecutionResult:
        targets.append(target)
        return ExecutionResult(cases=())

    monkeypatch.setattr(adapter.Executor, "execute", execute)
    monkeypatch.setattr(
        adapter.Evaluator,
        "evaluate",
        lambda *args: EvaluationResult(total_score=0, cases=()),
    )
    manage.smoke(tmp_path, "one")
    assert targets == [seed]
    assert not (tmp_path / "initial_target").exists()


def test_services_reject_missing_resources_before_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    from evolution_tasks.evoagentbench_research.scripts import services

    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests/resources.json").write_text(
        json.dumps(
            {
                "execution_code": "current-project",
                "files": [
                    {"path": "resources/missing", "bytes": 1, "sha256": "0" * 64}
                ],
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["services.py", str(tmp_path)])

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Service must not start before resource validation")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(FileNotFoundError, match="resources/missing"):
        services.main()
    assert not (tmp_path / ".local").exists()
