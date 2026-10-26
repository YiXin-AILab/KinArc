"""Export an original deployment into a new portable resource tree (read-only source).

Run with the original deployment's Python on the source Linux host. The output
contains data and private grading inputs and is NOT a public Git commit.
"""

import argparse
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_internal"))
if TYPE_CHECKING:
    from .._internal.portable import PATH_FIELDS, file_inventory, write_json
else:
    from portable import PATH_FIELDS, file_inventory, write_json


def project_source(path: Path) -> bool:
    """Exclude project implementations without discarding data inside a checkout."""
    for parent in (path, *path.parents):
        if (parent / "agentsystem").is_dir() and (parent / "evolution").is_dir():
            relative = path.relative_to(parent)
            return not relative.parts or relative.parts[0] in {
                "agentsystem",
                "evolution",
                "benchmarks",
                "application",
                "pyproject.toml",
                "uv.lock",
            }
    return False


def environment(python: str, destination: Path, name: str) -> None:
    """Record version pins; never export environment variables or credentials."""
    code = """
import importlib.metadata as m,json,sys
rows={}
for d in m.distributions():
    direct=json.loads(d.read_text('direct_url.json') or '{}')
    if direct.get('dir_info',{}).get('editable'): continue
    name=d.metadata.get('Name')
    if name: rows.setdefault(name,d.version)
try:
    import redis
    rows['redis']=redis.__version__
except ImportError: pass
print(json.dumps({'python':'.'.join(map(str,sys.version_info[:3])),'packages':rows}))
"""
    result = subprocess.run(
        [python, "-c", code], check=True, capture_output=True, text=True
    )
    value = json.loads(result.stdout)
    (destination / f"{name}-python.txt").write_text(value["python"] + "\n")
    (destination / f"{name}-requirements.txt").write_text(
        "".join(
            f"{k}=={v}\n"
            for k, v in sorted(value["packages"].items(), key=lambda x: x[0].lower())
        )
    )
    wheel_script = Path(__file__).resolve().parents[1] / "_internal/wheels.py"
    wheel_output = destination.parent / "dependencies/wheels" / name
    wheels = subprocess.run(
        [python, str(wheel_script), str(wheel_output)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    write_json(destination / f"{name}-vcs-wheels.json", json.loads(wheels.stdout))


def export(  # noqa: C901 - one owner records the complete frozen export boundary
    run: Path,
    upstream_python: str,
    output: Path,
    redis_image: str,
) -> None:
    """Copy known dependencies and sanitize connection configuration for relocation."""
    if output.exists():
        raise FileExistsError(output)
    run = run.resolve(strict=True)
    config = yaml.safe_load((run / "task/config.yaml").read_text())
    settings = config["settings"]
    upstream = Path(settings["upstream_repo"])
    domain = yaml.safe_load(Path(settings["domain_config"]).read_text())
    original_manifest = json.loads(Path(settings["dependency_manifest"]).read_text())
    if settings.get("hf_cache_dir", "").startswith("${"):
        caches = [
            p
            for p in original_manifest["directories"]
            if (Path(p) / "hub").is_dir() and (Path(p) / "datasets").is_dir()
        ]
        if len(caches) != 1:
            raise ValueError("Cannot resolve the original frozen HF cache")
        settings["hf_cache_dir"] = caches[0]
    output.mkdir(parents=True)
    configs = output / "configs"
    configs.mkdir()
    (output / "manifests").mkdir()
    dependencies = output / "dependencies"
    dependencies.mkdir()
    mapping = {}

    def bind(path: Path, relative: str) -> None:
        """Materialize symlinks so the package contains bytes, not host pointers."""
        original = str(path)
        if original in mapping:
            return
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(
                path,
                target,
                symlinks=False,
                ignore=shutil.ignore_patterns(
                    "__pycache__",
                    "*.pyc",
                    "*.lock",
                    ".DS_Store",
                ),
            )
        else:
            shutil.copy2(path, target, follow_symlinks=True)
        mapping[original] = relative

    # Git archive captures committed upstream resources and its license without
    # exporting remote URLs or any local Git credentials/config.
    archive = subprocess.check_output(["git", "-C", str(upstream), "archive", "HEAD"])
    target = dependencies / "upstream"
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        members = [
            member
            for member in stream.getmembers()
            if member.name.startswith("benchmark/")
            or Path(member.name).name in {"LICENSE", "NOTICE", "README.md"}
        ]
        stream.extractall(target, members=members, filter="data")
    revision = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    (target / "REVISION").write_text(revision + "\n")
    mapping[str(upstream)] = "dependencies/upstream"
    # Project execution code always comes from the current checkout. Never
    # export a private worker or public Runtime fork as a resource.
    settings.pop("research_runtime_source", None)

    directories = []
    for index, original in enumerate(original_manifest["directories"]):
        if any(original == old or original.startswith(old + "/") for old in mapping):
            continue
        if (
            project_source(Path(original))
            or "worker-deps" in original
            or "site-packages" in original
        ):
            continue
        if any(original.startswith(old + "/") for old in mapping):
            continue
        if original == settings.get("hf_cache_dir"):
            relative = "resources/hf-cache"
        elif Path(original).name == "Tevatron___browsecomp-plus-corpus":
            relative = "resources/hf-datasets/Tevatron___browsecomp-plus-corpus"
        else:
            relative = f"resources/data-{index:02d}/{Path(original).name}"
        bind(Path(original), relative)
        directories.append(relative)
    # Explicitly frozen standalone inputs include SQLite indexes and grading files.
    for index, row in enumerate(original_manifest["files"]):
        original = row["path"]
        if any(original == old or original.startswith(old + "/") for old in mapping):
            continue
        if (
            project_source(Path(original))
            or "worker-deps" in original
            or "site-packages" in original
            or original.endswith(".lock")
        ):
            continue
        bind(Path(original), f"resources/files/{index:05d}-{Path(original).name}")
    for key in ("dataset_file", "split_file", "supplemental_reference_manifest"):
        if domain.get(key) and not any(
            domain[key] == old or domain[key].startswith(old + "/") for old in mapping
        ):
            bind(Path(domain[key]), f"resources/{key}/{Path(domain[key]).name}")
    if settings.get("research_model_contract"):
        bind(Path(settings["research_model_contract"]), "configs/model-contract.yaml")

    def relative_paths(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: relative_paths(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [relative_paths(v, key) for v in value]
        if isinstance(value, str) and key in PATH_FIELDS and value.startswith("/"):
            for old, new in sorted(mapping.items(), key=lambda pair: -len(pair[0])):
                if value == old or value.startswith(old + "/"):
                    return new + value[len(old) :]
            raise ValueError(f"Unmapped path in {key}")
        return value

    settings.update(
        upstream_python="dependencies/upstream/.venv/bin/python",
        domain_config="configs/domain.yaml",
        runtime_config="configs/runtime.yaml",
        dependency_manifest=".local/dependency-manifest.json",
        artifact_dir=".local/evidence",
    )
    if "research_site_packages" in settings:
        settings["research_site_packages"] = ".local/container-python"
        settings["research_proxy_scopes"] = ".local/model-scopes"
        settings["research_proxy_url"] = "http://127.0.0.1:${EAB_PUBLIC_MODEL_PORT}/v1"
    domain["workspace_root"] = ".local/workspaces"
    domain = relative_paths(domain)
    # Grader endpoints and names remain explicit environment inputs.
    for key, variable in {
        "eval_api_base": "EAB_JUDGE_BASE_URL",
        "eval_api_key": "EAB_JUDGE_API_KEY",
        "eval_model_name": "EAB_JUDGE_MODEL",
    }.items():
        if key in domain:
            domain[key] = "${" + variable + "}"
    if "judge" in domain:
        domain["judge"].update(
            model="${EAB_JUDGE_MODEL}",
            api_base="${EAB_JUDGE_BASE_URL}",
            api_key="${EAB_JUDGE_API_KEY}",
        )
    runtime_config = yaml.safe_load((run / "runtime.yaml").read_text())
    runtime_config["redis"].update(
        host="${EAB_REDIS_HOST}",
        port="${EAB_REDIS_PORT}",
        password="${EAB_REDIS_PASSWORD}",
    )
    runtime_config["redis"]["username"] = "${EAB_REDIS_USERNAME}"
    runtime_config["trace"] = {"enabled": False}
    if runtime_config.get("asset_store", {}).get("type") == "local":
        runtime_config["asset_store"]["root"] = ".local/assets"
    for name, value in [("domain", domain), ("runtime", runtime_config)]:
        (configs / f"{name}.yaml").write_text(
            yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        )
    write_json(configs / "settings.json", relative_paths(settings))
    environment(upstream_python, configs, "upstream")
    services = {"redis_image": redis_image}
    if settings["domain"] == "information_retrieval":
        indexes = [new for old, new in mapping.items() if old.endswith("corpus.sqlite")]
        if len(indexes) != 1:
            raise ValueError("Cannot identify frozen lexical index")
        services["lexical_index"] = indexes[0]
    write_json(configs / "services.json", services)
    rows = file_inventory(output)
    write_json(
        output / "manifests/resources.json",
        {
            "schema_version": 2,
            "execution_code": "current-project",
            "files": rows,
            "directories": directories,
            "images": original_manifest["images"],
            "upstream_revision": revision,
            "platform": "linux-x86_64",
        },
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "files": len(rows),
                "bytes": sum(r["bytes"] for r in rows),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--upstream-python", required=True)
    parser.add_argument("--redis-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(
        args.run,
        args.upstream_python,
        args.output,
        args.redis_image,
    )
