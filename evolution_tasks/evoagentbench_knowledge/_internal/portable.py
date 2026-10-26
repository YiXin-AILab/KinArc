"""Relocatable resources and byte-preserving historical migration.

This module uses only the standard library so that resource checks work before
installing third-party grader dependencies.
"""

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

PATH_FIELDS = frozenset(
    {
        "upstream_repo",
        "upstream_python",
        "domain_config",
        "runtime_config",
        "dependency_manifest",
        "artifact_dir",
        "hf_cache_dir",
        "official_agent_config",
        "research_runtime_source",
        "research_site_packages",
        "research_model_contract",
        "research_proxy_scopes",
        "dataset_file",
        "split_file",
        "index_path",
        "model_name",
        "reference_dir",
        "meta_prompts_dir",
        "parquet_file",
        "workspace_root",
        "supplemental_reference_manifest",
    }
)


def sha256(path: Path) -> str:
    """Hash file bytes without loading large execution traces into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_path(root: Path, value: str) -> Path:
    """Reject task resources which escape through .. or symbolic links."""
    result = (root / value).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Resource outside task: {value}")
    return result


def write_json(path: Path, value: Any) -> None:
    """Atomically write private, structured metadata; never expose credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def file_inventory(root: Path) -> list[dict[str, Any]]:
    """Inventory regular files, rejecting hidden external resource dependencies."""
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink requires explicit materialization: {path}")
        if path.is_file():
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    return rows


def require_current_project(root: Path, manifest: dict[str, Any]) -> None:
    """Reject legacy resource bundles that embed a second project implementation."""
    if manifest.get("execution_code") != "current-project":
        raise ValueError(
            "Legacy execution bundle: re-export resources for current-project execution"
        )
    for prefix in ("runtime-worker", "public-runtime"):
        if (root / "dependencies" / prefix).exists():
            raise ValueError(
                "Remove archived project copy from task dependencies: " + prefix
            )
    for row in manifest["files"]:
        parts = Path(row["path"]).parts
        if "agentsystem" in parts or "evolution" in parts:
            raise ValueError("Project implementation cannot be a task resource")


def verify_files(
    root: Path, rows: list[dict[str, Any]], *, check_hashes: bool = True
) -> None:
    """Check declared files; historical evidence also requires exact content."""
    missing = [
        row["path"] for row in rows if not local_path(root, row["path"]).is_file()
    ]
    if missing:
        raise FileNotFoundError("Missing resources:\n" + "\n".join(missing))
    seen = set()
    for row in rows:
        path = local_path(root, row["path"])
        if row["path"] in seen:
            raise ValueError("Duplicate resource path")
        seen.add(row["path"])
        if not path.is_file():
            raise FileNotFoundError(f"Missing resource: {row['path']}")
        if check_hashes and (
            path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]
        ):
            raise ValueError(f"Resource hash changed: {row['path']}")


def relocate(value: Any, root: Path, mapping: dict[str, str], key: str = "") -> Any:
    """Rewrite only known configuration paths, never prompts or raw evidence."""
    if isinstance(value, dict):
        return {k: relocate(v, root, mapping, k) for k, v in value.items()}
    if isinstance(value, list):
        return [relocate(v, root, mapping, key) for v in value]
    if not isinstance(value, str) or key not in PATH_FIELDS:
        return value
    if "${" in value:
        raise ValueError(f"Resource paths cannot depend on environment: {key}")
    if not value.startswith("/"):
        if key == "upstream_python":
            path = root / value
            local_path(root, str(Path(value).parent))
            return str(path.absolute())
        return str(local_path(root, value))
    for old, new in sorted(mapping.items(), key=lambda pair: -len(pair[0])):
        if value == old or value.startswith(old.rstrip("/") + "/"):
            suffix = value[len(old) :].lstrip("/")
            return str(local_path(root, str(Path(new) / suffix)))
    raise ValueError(f"Unmapped external path in {key}")


def check_dataset(path: Path) -> list[str]:
    """Reject empty test placeholders and duplicate case IDs before dispatch."""
    cases = json.loads(path.read_text())["cases"]
    ids = [case["id"] for case in cases]
    if not ids:
        raise ValueError(f"Dataset is empty; deferred test cannot run: {path.name}")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case IDs")
    return ids


def history_inventory(candidates: Path, previous: dict[str, Any]) -> dict[str, Any]:
    """Bind current files to historical digests without retaining old file copies."""
    original_rows = previous.get("source_files", previous.get("files", []))
    original = {row["path"]: row for row in original_rows}
    previous_changes = {row["path"]: row for row in previous.get("changed", [])}
    files = []
    changes = []
    mutable = {row["path"] for row in previous.get("mutable_notes", [])}
    for row in file_inventory(candidates):
        if not re.fullmatch(r"candidate-\d{8}T\d{6}", row["path"].split("/")[0]):
            raise ValueError("Candidate directory contains an extra non-candidate file")
        path = candidates / row["path"]
        prior = original.get(row["path"], row)
        before = prior.get("source_sha256", prior["sha256"])
        conversion = previous_changes.get(row["path"])
        if conversion and row["sha256"] != conversion["migrated_sha256"]:
            raise ValueError(f"Previously migrated file changed: {row['path']}")
        if (
            row["path"] in original
            and not conversion
            and row["path"] not in mutable
            and row["sha256"] != prior["sha256"]
        ):
            raise ValueError(f"Historical file changed: {row['path']}")
        if path.name == "evaluation.json":
            value = json.loads(path.read_text())
            if "challenge_comparison" in value:
                if "parent_comparison" in value:
                    raise ValueError("Ambiguous evaluation schema")
                value["parent_comparison"] = value.pop("challenge_comparison")
                write_json(path, value)
                row.update(bytes=path.stat().st_size, sha256=sha256(path))
        row["source_sha256"] = before
        files.append(row)
        if before != row["sha256"]:
            changes.append(
                {
                    "path": row["path"],
                    "source_sha256": before,
                    "migrated_sha256": row["sha256"],
                    "change": (conversion or {}).get(
                        "change",
                        "evaluation schema or existing changelog conversion",
                    ),
                }
            )
    return {
        "schema_version": 2,
        "candidate_count": len({row["path"].split("/")[0] for row in files}),
        "files": files,
        "changed": changes,
        "rollback_policy": "historical digests linked to verified current files",
    }


def migrate_candidates(source: Path, destination: Path) -> dict[str, Any]:
    """Import the earliest twenty candidates without code or evidence backups."""
    if destination.exists():
        raise FileExistsError(destination)
    candidates = sorted(
        p
        for p in source.iterdir()
        if p.is_dir() and re.fullmatch(r"candidate-\d{8}T\d{6}", p.name)
    )[:20]
    if not candidates:
        raise ValueError("No candidate history found")
    ids = {p.name for p in candidates}
    for candidate in candidates:
        parent = candidate / "parent"
        if parent.exists() and parent.read_text().strip() not in ids | {""}:
            raise ValueError("Retained candidate has a missing parent")
        rollback = candidate / "rollback.json"
        if rollback.exists():
            value = json.loads(rollback.read_text())
            for key in ("source_candidate_id", "changelog_source_candidate_id"):
                if key in value and value[key] not in ids:
                    raise ValueError("Retained rollback has a missing source")
    previous = source.parent.parent / "manifests/history-migration.json"
    migration = json.loads(previous.read_text()) if previous.exists() else {}
    for candidate in candidates:
        shutil.copytree(candidate, destination / candidate.name)
    return history_inventory(destination, migration)


def rollback_hash(
    root: Path, migration: dict[str, Any], candidate_id: str, path: Path
) -> str:
    """Resolve a historical digest only after verifying the current artifact."""
    files = {row["path"]: row for row in migration["files"]}
    relative = str(path.relative_to(root / ".evolution/candidates"))
    if f"{candidate_id}/rollback.json" in files and relative in files:
        row = files[relative]
        verify_files(root / ".evolution/candidates", [row])
        return str(row.get("source_sha256", row["sha256"]))
    return sha256(path)
