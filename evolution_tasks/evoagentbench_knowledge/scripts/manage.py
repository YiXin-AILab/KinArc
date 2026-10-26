"""Task-local migration, resource auditing, environment setup and explicit runs."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

TEMPLATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE / "_internal"))

if TYPE_CHECKING:
    from .._internal.portable import (
        migrate_candidates,
        require_current_project,
        rollback_hash,
        sha256,
        verify_files,
        write_json,
    )
else:
    from portable import (
        migrate_candidates,
        require_current_project,
        rollback_hash,
        sha256,
        verify_files,
        write_json,
    )


def pending_evaluation(
    execution: object | None, evaluation: object | None, historical: bool
) -> bool:
    """Allow a new execution to resume aggregation, never incomplete imports."""
    if execution is None and evaluation is not None:
        raise ValueError("Evaluation without execution requires explicit repair")
    if execution is not None and evaluation is None:
        if historical:
            raise ValueError("Partial historical result requires explicit repair")
        return True
    return False


def validate_history(root: Path) -> dict[str, Any]:
    """Validate parents, rollback provenance and model schemas without execution."""
    import yaml
    from evolution.candidates import CandidateRepository, tree_digest
    from evolution.models import DatasetRole

    config = yaml.safe_load((root / "config.yaml").read_text())
    repository = CandidateRepository(
        root,
        workspace_root=root / ".local/workspaces-current",
        initial_target=root / config["initial_target"],
        objective=config["objective"],
        read_only=tuple(config.get("workspace", {}).get("read_only", [])),
        inaccessible=tuple(config.get("workspace", {}).get("inaccessible", [])),
    )
    candidates = repository.list_candidates()
    migration: dict[str, Any] = json.loads(
        (root / "manifests/history-migration.json").read_text()
    )
    original_ids = {row["path"].split("/")[0] for row in migration["files"]}
    pending_evaluations = 0
    seen = set()
    for candidate in candidates:
        if candidate.parent_id is not None and candidate.parent_id not in seen:
            raise ValueError("Missing or unordered historical parent")
        seen.add(candidate.id)
        for role in (DatasetRole.TRAIN, DatasetRole.VALIDATION):
            execution = repository.load_execution(candidate.id, role)
            evaluation = repository.load_evaluation(candidate.id, role)
            pending_evaluations += pending_evaluation(
                execution, evaluation, candidate.id in original_ids
            )
        rollback = repository.load_rollback(candidate.id)
        if rollback:
            repository.validate_rollback_results(candidate.id)
            source = repository.load_candidate(rollback.source_candidate_id)
            if tree_digest(source.target_dir) != rollback.source_target_sha256:
                raise ValueError("Rollback source target differs")
            for role, hashes in rollback.reused_artifacts.items():
                for kind in ("execution", "evaluation"):
                    path = source.directory / role.value / f"{kind}.json"
                    original = rollback_hash(root, migration, candidate.id, path)
                    if original != getattr(hashes, kind + "_sha256"):
                        raise ValueError("Rollback original artifact hash differs")
    validate_problem_library(root, seen)
    return {
        "candidates": len(candidates),
        "schema_and_lineage": "verified",
        "pending_evaluations": pending_evaluations,
    }


def validate_problem_library(root: Path, seen: set[str]) -> None:
    """Validate active problem provenance against retained training data."""
    from evolution.diagnosis import ProblemLibrary

    library = ProblemLibrary.model_validate_json(
        (root / ".evolution/problem_library.json").read_text()
    )
    train_ids = {
        case["id"]
        for case in json.loads((root / "datasets/train.json").read_text())["cases"]
    }
    for entry in library.entries:
        if (
            entry.first_seen_candidate_id not in seen
            or entry.last_seen_candidate_id not in seen
        ):
            raise ValueError("Problem library references a removed candidate")
        if not set(entry.case_ids) <= train_ids:
            raise ValueError("Problem library references cases outside training")


def require_history(source_task: Path) -> None:
    """Reject incomplete downloads before copying large resources."""
    for name in (
        "config.yaml",
        "datasets/train.json",
        "datasets/validation.json",
        ".evolution/manifest.json",
    ):
        if not (source_task / name).is_file():
            raise FileNotFoundError(f"Incomplete historical task: missing {name}")
    if not (source_task / ".evolution/candidates").is_dir():
        raise FileNotFoundError("Incomplete historical task: missing candidates")


def assemble(source: Path, resources: Path, destination: Path) -> None:
    """Create a new task without deleting or modifying the historical run."""
    import yaml

    source = source.resolve(strict=True)
    resources = resources.resolve(strict=True)
    if destination.exists():
        raise FileExistsError(destination)
    if destination.resolve().is_relative_to(source):
        raise ValueError("Destination must be separate from the historical run")
    source_task = source / "task" if (source / "task").is_dir() else source
    require_history(source_task)
    seed_id = json.loads((source_task / ".evolution/manifest.json").read_text())[
        "seed_candidate_id"
    ]
    retained = sorted((source_task / ".evolution/candidates").glob("candidate-*"))[:20]
    if not retained or seed_id != retained[0].name:
        raise ValueError("Manifest seed must be the earliest retained candidate")
    if (retained[0] / "parent").read_text().strip():
        raise ValueError("Historical seed must not have a parent")
    if destination.resolve().parent != TEMPLATE.parent:
        raise ValueError("Destination must share the public template Tasks directory")
    resource_manifest = json.loads((resources / "manifests/resources.json").read_text())
    require_current_project(resources, resource_manifest)
    verify_files(resources, resource_manifest["files"], check_hashes=False)
    destination.mkdir(parents=True)
    for name in ("dependencies", "resources", "configs"):
        shutil.copytree(resources / name, destination / name)
    for name in ("resources.json", "test-reference-files.json"):
        path = resources / "manifests" / name
        if path.is_file():
            write_json(destination / "manifests" / name, json.loads(path.read_text()))
    shutil.copytree(source_task / "datasets", destination / "datasets")
    migration = migrate_candidates(
        source_task / ".evolution/candidates", destination / ".evolution/candidates"
    )
    original_config = source_task / "config.yaml"
    config = yaml.safe_load(original_config.read_text())
    config.pop("max_candidates", None)
    config["max_iterations"] = 0
    config["max_retries"] = 0
    config["initial_target"] = f".evolution/candidates/{seed_id}/target"
    config["executor"] = os.path.relpath(TEMPLATE / "executor.py", destination)
    config["evaluator"] = os.path.relpath(TEMPLATE / "evaluator.py", destination)
    config["settings"] = {"portable": True}
    (destination / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    )
    write_json(destination / "manifests/history-migration.json", migration)
    # Only the experiment identity is persistent engine state in this protocol.
    # Never import obsolete iteration/incumbent counters or live runtime state.
    write_json(
        destination / ".evolution/manifest.json",
        {
            "seed_candidate_id": seed_id,
            "objective": config["objective"],
            "read_only": config.get("workspace", {}).get("read_only", []),
            "inaccessible": config.get("workspace", {}).get("inaccessible", []),
        },
    )
    rebuild_problem_library(destination)
    refresh_resource_metadata(destination)
    validate_history(destination)
    print(
        json.dumps(
            {
                "task": str(destination),
                "candidates": migration["candidate_count"],
                "status": "assembled; environment and live validation still required",
            }
        )
    )


def verify_history_files(root: Path) -> dict[str, Any]:
    """Check current bytes and the migration digest chain; no old copies needed."""
    migration: dict[str, Any] = json.loads(
        (root / "manifests/history-migration.json").read_text()
    )
    if migration["schema_version"] != 2:
        raise ValueError("History must use the digest-only migration ledger")
    rows = {row["path"]: row for row in migration["files"]}
    changes = {row["path"]: row for row in migration["changed"]}
    if len(rows) != len(migration["files"]) or len(changes) != len(
        migration["changed"]
    ):
        raise ValueError("Duplicate migration path")
    if migration["candidate_count"] != len({p.split("/")[0] for p in rows}):
        raise ValueError("Migration candidate count differs")
    for path, row in rows.items():
        change = changes.pop(path, None)
        if row["source_sha256"] != row["sha256"]:
            if not change or (change["source_sha256"], change["migrated_sha256"]) != (
                row["source_sha256"],
                row["sha256"],
            ):
                raise ValueError("Migration digest chain differs")
        elif change:
            raise ValueError("Unnecessary conversion record")
    if changes:
        raise ValueError("Conversion references a removed file")
    verify_files(root / ".evolution/candidates", migration["files"])
    return migration


def audit(root: Path) -> dict[str, Any]:
    """Check every required payload and migrated history without calling models."""
    manifest = json.loads((root / "manifests/resources.json").read_text())
    require_current_project(root, manifest)
    verify_files(root, manifest["files"], check_hashes=False)
    migration = verify_history_files(root)
    return {
        "status": "offline_verified",
        "resource_files": len(manifest["files"]),
        "historical_candidates": migration["candidate_count"],
        "live_execution_verified": False,
    }


def rebuild_problem_library(root: Path) -> None:
    """Rebuild only from retained training observations, in candidate order."""
    import yaml
    from evolution.diagnosis import (
        ProblemLibrary,
        build_diagnosis_brief,
        update_problem_library,
    )
    from evolution.models import EvaluationResult

    config = yaml.safe_load((root / "config.yaml").read_text())
    library = ProblemLibrary()
    for path in sorted(
        (root / ".evolution/candidates").glob("candidate-*/train/evaluation.json")
    ):
        evaluation = EvaluationResult.model_validate_json(path.read_text())
        brief = build_diagnosis_brief(
            evidence_candidate_id=path.parents[1].name, evaluation=evaluation
        )
        library = update_problem_library(
            library, brief, min_cases=config.get("single_case_min_cases", 2)
        )
    write_json(
        root / ".evolution/problem_library.json", library.model_dump(mode="json")
    )


def refresh_resource_metadata(root: Path) -> None:
    """Update only assembled configuration/dataset digests, never evidence bytes."""
    path = root / "manifests/resources.json"
    manifest = json.loads(path.read_text())
    manifest.pop("source_run", None)
    for row in manifest["files"]:
        if Path(row["path"]).parts[0] in {"configs", "datasets"}:
            resource = root / row["path"]
            row.update(bytes=resource.stat().st_size, sha256=sha256(resource))
    write_json(path, manifest)


def install(root: Path) -> None:
    """Install the third-party grader and current project container dependencies."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("Task installation requires a Linux amd64 host")
    if TYPE_CHECKING:
        from .._internal.service_docker import service_environment
    else:
        from service_docker import service_environment

    audit(root)
    for archive in sorted((root / "dependencies/images").glob("*.tar")):
        subprocess.run(
            ["docker", "image", "load", "--input", str(archive)],
            check=True,
            timeout=600,
            env=service_environment() if archive.name == "redis.tar" else None,
        )
    for name in ("upstream",):
        dependency = root / "dependencies" / name
        (root / "dependencies/wheels" / name).mkdir(parents=True, exist_ok=True)
        lock = root / "configs" / f"{name}-requirements.txt"
        version = (root / "configs" / f"{name}-python.txt").read_text().strip()
        python = dependency / ".venv/bin/python"
        if python.is_file():
            installed = subprocess.check_output(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import sys; print('.'.join(map(str, sys.version_info[:3])))",
                ],
                text=True,
                timeout=30,
            ).strip()
            if installed != version:
                raise ValueError(f"{name}: existing interpreter version differs")
        else:
            subprocess.run(
                ["uv", "venv", "--python", version, str(dependency / ".venv")],
                check=True,
                timeout=600,
            )
        subprocess.run(
            [
                "uv",
                "pip",
                "sync",
                "--find-links",
                str(root / "dependencies/wheels" / name),
                "--python",
                str(dependency / ".venv/bin/python"),
                str(lock),
            ],
            check=True,
            timeout=1800,
        )
    settings = json.loads((root / "configs/settings.json").read_text())
    if settings["domain"] == "information_retrieval":
        # Source-only dependencies need the container Python version.
        # Install from the current lock, without wheel-only cross-resolution.
        image = settings["research_execution_image"]
        identity = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--rm",
                    image,
                    "python",
                    "-c",
                    "import platform,json;print(json.dumps([platform.machine(),"
                    "platform.libc_ver(),platform.python_version()]))",
                ],
                text=True,
                timeout=60,
            )
        )
        if identity[0] != "x86_64" or identity[1][0] != "glibc":
            raise ValueError("Frozen retrieval image requires Linux amd64/glibc")
        if tuple(map(int, identity[2].split(".")[:2])) < (3, 13):
            raise ValueError("Current Runtime requires a Python 3.13+ container image")
        from benchmarks.evoagentbench.common.identity import file_digest, project_root

        lock = root / ".local/current-runtime-requirements.txt"
        lock.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(lock),
            ],
            cwd=project_root(),
            check=True,
            timeout=120,
        )
        packages = root / ".local/container-python"
        python_abi = ".".join(identity[2].split(".")[:2])
        subprocess.run(["uv", "python", "install", python_abi], check=True, timeout=600)
        subprocess.run(
            [
                "uv",
                "pip",
                "sync",
                "--target",
                str(packages),
                "--require-hashes",
                "--python",
                python_abi,
                str(lock),
            ],
            check=True,
            timeout=1800,
        )
        write_json(
            root / ".local/container-install.json",
            {
                "project_lock_sha256": file_digest(project_root() / "uv.lock"),
                "requirements_sha256": file_digest(lock),
                "image": image,
                "container_python": identity[2],
            },
        )
    print("Environments installed. Run check, services and smoke before resume.")


def smoke(root: Path, case_id: str) -> None:
    """Execute and grade one training case in an isolated acceptance ledger."""
    import importlib.util

    from evolution.models import DatasetRole
    from evolution.task import load_task_config

    config = load_task_config(root)
    status = json.loads((root / ".local/services.json").read_text())
    if status["status"] != "ready":
        raise ValueError("Start task services and wait for readiness")
    dataset = json.loads((root / "datasets/train.json").read_text())
    cases = [case for case in dataset["cases"] if case["id"] == case_id]
    if len(cases) != 1:
        raise ValueError("Smoke case must belong to the original train split")
    destination = root / ".local/acceptance"
    destination.mkdir(parents=True, exist_ok=True)
    dataset["cases"] = cases
    subset = destination / "train-one.json"
    write_json(subset, dataset)
    spec = importlib.util.spec_from_file_location(
        "_portable_loader", TEMPLATE / "_internal/loading.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load task-local loader")
    loading = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loading)
    adapter = loading.adapter(TEMPLATE / "executor.py")
    execution = adapter.Executor(root, {"portable": True}).execute(
        config.initial_target,
        subset,
        DatasetRole.TRAIN,
        max_retries=0,
    )
    write_json(destination / "execution.json", execution.model_dump(mode="json"))
    evaluation = adapter.Evaluator(root, {"portable": True}).evaluate(execution, subset)
    write_json(destination / "evaluation.json", evaluation.model_dump(mode="json"))
    print(
        json.dumps(
            {
                "case_id": case_id,
                "score": evaluation.total_score,
                "execution": str(destination / "execution.json"),
            }
        )
    )


def restore_check(root: Path) -> None:
    """Run the current engine's zero-budget restore with all model ports forbidden."""
    import asyncio
    from dataclasses import replace

    from evolution.candidates import CandidateRepository
    from evolution.engine import EvolutionEngine
    from evolution.task import load_task, load_task_config

    class Forbidden:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Historical restore requested a new execution")

        def evaluate(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Historical restore requested regrading")

        async def improve(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Zero-budget restore requested an improvement")

    verify_history_files(root)
    validate_history(root)
    config = load_task_config(root)
    task = replace(load_task(config), executor=Forbidden(), evaluator=Forbidden())
    repository = CandidateRepository(
        root,
        workspace_root=root / ".local/current-workspaces",
        initial_target=config.initial_target,
        objective=config.objective,
        read_only=config.workspace.read_only,
        inaccessible=config.workspace.inaccessible,
    )
    engine = EvolutionEngine(
        repository=repository,
        task=task,
        improver=Forbidden(),
        objective=config.objective,
        read_only=config.workspace.read_only,
        inaccessible=config.workspace.inaccessible,
        max_iterations=0,
        max_retries=0,
        retry_delay_seconds=0,
        rollback_enabled=config.rollback_enabled,
        exploration_patience=config.exploration_patience,
        rollback_changelog_source=config.rollback_changelog_source,
    )
    result = asyncio.run(engine.run())
    verify_history_files(root)
    destination = root / ".local/acceptance/restore.json"
    write_json(destination, result.model_dump(mode="json"))
    print(result.model_dump_json(indent=2))


def resume(args: argparse.Namespace) -> None:
    """Start an explicitly budgeted continuation after offline and service checks."""
    import asyncio
    import fcntl

    import yaml

    root = args.task_dir.resolve()
    if args.additional_iterations == 0:
        restore_check(root)
        return
    if args.additional_iterations < 0:
        raise ValueError("Additional iterations must be nonnegative")
    local = root / ".local"
    local.mkdir(exist_ok=True)
    lock = (local / "run.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    audit(root)
    validate_history(root)
    status = json.loads((root / ".local/services.json").read_text())
    if status["status"] != "ready":
        raise ValueError("Task services are not ready")
    service_environment = root / ".local/services-env.json"
    if service_environment.exists():
        os.environ.update(json.loads(service_environment.read_text()))
    runtime_path = args.runtime_config
    if runtime_path is None:
        runtime_path = root / ".local/improver-runtime.yaml"
        runtime_path.write_text(
            yaml.safe_dump(
                {
                    "redis": {
                        "type": "standalone",
                        "host": "${EAB_REDIS_HOST}",
                        "port": "${EAB_REDIS_PORT}",
                        "username": "${EAB_REDIS_USERNAME}",
                        "password": "${EAB_REDIS_PASSWORD}",
                        "database": 0,
                        "key_prefix": "portable-evolution",
                    },
                    "asset": {"max_file_bytes": 104857600},
                    "asset_store": {
                        "type": "local",
                        "root": ".local/improver-assets",
                    },
                    "logging": {"directory": ".local/improver-logs"},
                    "trace": {"enabled": False},
                }
            )
        )
        runtime_path.chmod(0o600)
    try:
        asyncio.run(
            run_iterations(root, runtime_path.resolve(), args.additional_iterations)
        )
    finally:
        lock.close()


async def run_iterations(root: Path, runtime_path: Path, iterations: int) -> None:
    """Use the maintained public engine with an invocation-only new budget."""
    import hashlib

    from agentsystem.common.logging import setup_logging
    from agentsystem.config.loader import load_yaml_config
    from agentsystem.config.models import RuntimeConfig
    from evolution.agentsystem_improver import AgentSystemImprover
    from evolution.candidates import CandidateRepository
    from evolution.engine import EvolutionEngine
    from evolution.task import load_task, load_task_config

    config = load_task_config(root)
    runtime = load_yaml_config(runtime_path, RuntimeConfig, environ=os.environ)
    logging = setup_logging(runtime.logging, project_root=root)
    try:
        improver = await AgentSystemImprover.create(
            runtime_config=runtime,
            project_root=root,
            redis_prefix="eab-" + hashlib.sha256(str(root).encode()).hexdigest()[:16],
            request_timeout_seconds=config.improver_timeout_seconds,
            environ=os.environ,
        )
        async with improver:
            engine = EvolutionEngine(
                repository=CandidateRepository(
                    root,
                    workspace_root=root / ".local/current-workspaces",
                    initial_target=config.initial_target,
                    objective=config.objective,
                    read_only=config.workspace.read_only,
                    inaccessible=config.workspace.inaccessible,
                ),
                task=load_task(config),
                improver=improver,
                objective=config.objective,
                read_only=config.workspace.read_only,
                inaccessible=config.workspace.inaccessible,
                max_iterations=iterations,
                max_retries=config.max_retries,
                max_case_concurrency=config.max_case_concurrency,
                retry_delay_seconds=config.retry_delay_seconds,
                rollback_enabled=config.rollback_enabled,
                exploration_patience=config.exploration_patience,
                rollback_changelog_source=config.rollback_changelog_source,
                single_case_min_cases=config.single_case_min_cases,
            )
            result = await engine.run()
            write_json(
                root / ".local/optimization.json", result.model_dump(mode="json")
            )
            print(result.model_dump_json(indent=2))
    finally:
        logging.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("assemble")
    create.add_argument("--history", type=Path, required=True)
    create.add_argument("--resources", type=Path, required=True)
    create.add_argument("--destination", type=Path, required=True)
    for name in ("check", "install", "restore-check"):
        commands.add_parser(name).add_argument("task_dir", type=Path)
    smoke_command = commands.add_parser("smoke")
    smoke_command.add_argument("task_dir", type=Path)
    smoke_command.add_argument("--case-id", required=True)
    run = commands.add_parser("run")
    run.add_argument("task_dir", type=Path)
    run.add_argument("--additional-iterations", type=int, default=0)
    run.add_argument("--runtime-config", type=Path)
    args = parser.parse_args()
    if args.command != "assemble":
        args.task_dir = args.task_dir.resolve(strict=True)
    if args.command == "assemble":
        assemble(args.history, args.resources, args.destination)
    elif args.command == "check":
        print(
            json.dumps(
                {**audit(args.task_dir), **validate_history(args.task_dir)}, indent=2
            )
        )
    elif args.command == "install":
        install(args.task_dir)
    elif args.command == "restore-check":
        restore_check(args.task_dir)
    elif args.command == "smoke":
        smoke(args.task_dir, args.case_id)
    elif args.command == "run":
        resume(args)


if __name__ == "__main__":
    main()
