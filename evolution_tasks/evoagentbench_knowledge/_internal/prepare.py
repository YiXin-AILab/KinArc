"""Resolve only task-local resources and produce deterministic worker config."""

import fcntl
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from benchmarks.evoagentbench.common.identity import project_root, source_digest
from benchmarks.evoagentbench.domains.domain_dependencies import file_hash

if TYPE_CHECKING:
    from .environment import verify_environment
else:
    from environment import verify_environment
if TYPE_CHECKING:
    from .portable import (
        local_path,
        relocate,
        require_current_project,
        verify_files,
        write_json,
    )
else:
    from portable import (
        local_path,
        relocate,
        require_current_project,
        verify_files,
        write_json,
    )


def expand_environment(value: Any) -> Any:
    """Expand connection settings at runtime; never persist expanded secrets."""
    if isinstance(value, dict):
        return {k: expand_environment(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_environment(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ[m[1]], value)
    return value


def prepare(root: Path) -> dict[str, Any]:
    """Verify resources and resolve paths, recording a new relocation identity."""
    local = root / ".local"
    local.mkdir(parents=True, exist_ok=True)
    with (local / "prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = json.loads((root / "manifests/resources.json").read_text())
        require_current_project(root, manifest)
        verify_files(root, manifest["files"], check_hashes=False)
        manifest["files"] = [
            {
                **row,
                "bytes": local_path(root, row["path"]).stat().st_size,
                "sha256": file_hash(local_path(root, row["path"])),
            }
            for row in manifest["files"]
        ]
        verify_environment(root)
        # All replacement paths were made relative by the assembly step.
        settings = relocate(
            json.loads((root / "configs/settings.json").read_text()), root, {}
        )
        if settings["domain"] == "information_retrieval":
            settings["research_runtime_source"] = str(project_root())
            settings["research_site_packages"] = str(root / ".local/container-python")
            port = int(os.environ["EAB_PUBLIC_MODEL_PORT"])
            settings["research_proxy_url"] = f"http://127.0.0.1:{port}/v1"
        if settings.get("hf_cache_dir"):
            cache = local / "hf-cache"
            if not cache.exists():
                staging = local / "hf-cache.staging"
                shutil.copytree(settings["hf_cache_dir"], staging, dirs_exist_ok=True)
                staging.rename(cache)
            source_cache = Path(settings["hf_cache_dir"])
            cache_rows = []
            for row in manifest["files"]:
                source = root / row["path"]
                if source.is_relative_to(source_cache):
                    cache_rows.append(
                        {**row, "path": str(source.relative_to(source_cache))}
                    )
            verify_files(cache, cache_rows)
            os.environ["EAB_TASK_HF_CACHE"] = str(cache)
        domain = relocate(
            yaml.safe_load((root / "configs/domain.yaml").read_text()), root, {}
        )
        if "mcp_server" in domain:
            port = int(os.environ["EAB_PUBLIC_MCP_PORT"])
            domain["mcp_server"].update(port=port, url=f"http://127.0.0.1:{port}/mcp")
        runtime = yaml.safe_load((root / "configs/runtime.yaml").read_text())
        for name, value in [("domain", domain), ("runtime", runtime)]:
            destination = local / f"{name}.yaml"
            content = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
            destination.write_text(content)
            destination.chmod(0o600)
        settings["domain_config"] = str(local / "domain.yaml")
        settings["runtime_config"] = str(local / "runtime.yaml")
        settings["dependency_manifest"] = str(local / "dependency-manifest.json")
        settings["artifact_dir"] = str(local / "evidence")
        dependencies = {
            "schema_version": 1,
            "files": [
                {
                    "path": str(local_path(root, r["path"])),
                    "resolved_path": str(local_path(root, r["path"])),
                    "sha256": r["sha256"],
                    "bytes": r["bytes"],
                }
                for r in manifest["files"]
            ],
            "directories": [
                str(local_path(root, p)) for p in manifest.get("directories", [])
            ],
            "images": manifest.get("images", {}),
            "external_services": {},
        }
        write_json(local / "dependency-manifest.json", dependencies)
        # Interpreter symlinks point at the installed Python by design; resource
        # files themselves must remain contained and content-checked.
        for key in ("upstream_python",):
            if not Path(settings[key]).is_file():
                raise FileNotFoundError(f"Missing installed interpreter: {key}")
        identity = hashlib.sha256(
            json.dumps(
                {
                    "resources": manifest,
                    "maintained_project_sha256": source_digest(),
                    "settings": expand_environment(settings),
                    "domain": expand_environment(domain),
                    "runtime": expand_environment(runtime),
                    "integration": {
                        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(Path(__file__).parent.glob("*.py"))
                    },
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        write_json(
            local / "relocation.json",
            {
                "protocol_id": identity,
                "root": str(root),
                "historical_scores": "historical observations; not newly measured",
                "resources_verified": len(manifest["files"]),
            },
        )
        expanded: dict[str, Any] = expand_environment(settings)
        return expanded
