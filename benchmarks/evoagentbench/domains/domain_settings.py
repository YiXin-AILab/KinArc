"""办公和信息检索官方领域的显式配置。"""

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from agentsystem.common.models import JsonValue, StrictModel
from pydantic import Field

from benchmarks.evoagentbench.common.identity import source_digest
from benchmarks.evoagentbench.data.models import UPSTREAM_REVISION
from benchmarks.evoagentbench.domains.domain_dependencies import validate


class DomainSettings(StrictModel):
    """将上游私有配置保留在冻结候选目录之外。"""

    domain: Literal["knowledge_work", "information_retrieval"]
    upstream_repo: Path
    upstream_python: Path
    domain_config: Path
    runtime_config: Path
    upstream_revision: str = UPSTREAM_REVISION
    execution_image: str = "ea-bench-office:local"
    docker_socket: str = "/var/run/docker.sock"
    artifact_dir: Path | None = None
    dependency_manifest: Path | None = None
    hf_cache_dir: Path | None = None
    research_execution_image: str | None = None
    research_runtime_source: Path | None = None
    research_model_contract: Path | None = None
    research_site_packages: Path | None = None
    research_proxy_scopes: Path | None = None
    research_proxy_url: str | None = None
    runner: Literal["runtime", "official_baseline"] = "runtime"
    official_agent_config: Path | None = None
    case_timeout_seconds: float = Field(default=1800, gt=0)
    verifier_timeout_seconds: float = Field(default=1800, gt=0)
    train_trials: int = Field(default=1, ge=1)
    validation_trials: int = Field(default=1, ge=1)
    test_trials: int = Field(default=1, ge=1)


def settings_for(root: Path, raw: Mapping[str, JsonValue]) -> DomainSettings:
    """解析依赖路径，同时保留上游虚拟环境可执行文件路径。"""
    settings = DomainSettings.model_validate(dict(raw))
    updates: dict[str, object] = {}
    for name in (
        "upstream_repo",
        "domain_config",
        "runtime_config",
        "official_agent_config",
        "artifact_dir",
        "dependency_manifest",
        "hf_cache_dir",
        "research_model_contract",
        "research_runtime_source",
        "research_site_packages",
        "research_proxy_scopes",
    ):
        value = getattr(settings, name)
        if value is not None:
            updates[name] = (root / value).resolve()
    updates["upstream_python"] = (root / settings.upstream_python).absolute()
    if settings.domain == "knowledge_work":
        updates["execution_image"] = _image_id(settings.execution_image)
    if settings.research_execution_image:
        if settings.domain != "information_retrieval" or not all(
            (
                settings.research_model_contract,
                settings.research_runtime_source,
                settings.research_site_packages,
                settings.research_proxy_scopes,
                settings.research_proxy_url,
            )
        ):
            raise ValueError(
                "Isolated research requires image, model contract, packages and proxy"
            )
        updates["research_execution_image"] = _image_id(
            settings.research_execution_image
        )
    return settings.model_copy(update=updates)


def _resolved_config(path: Path) -> Any:
    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                lambda match: os.environ[match[1]],
                value,
            )
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return resolve(yaml.safe_load(path.read_text()))


def dependency_identity(settings: DomainSettings) -> str:
    """对实际官方源码、解析后的配置和冻结数据证据计算摘要。"""
    digest = hashlib.sha256()
    digest.update(source_digest().encode())
    source = settings.upstream_repo / "benchmark/src"
    if not source.is_dir():
        raise ValueError("Official source is unavailable")
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".js", ".md", ".yaml"}:
            digest.update(str(path.relative_to(source)).encode())
            digest.update(path.read_bytes())
    domain = _resolved_config(settings.domain_config)
    for config in (domain, _resolved_config(settings.runtime_config)):
        digest.update(json.dumps(config, sort_keys=True).encode())
    digest.update(_data_identity(settings.domain_config.parent, domain).encode())
    if settings.dependency_manifest:
        manifest = validate(settings.dependency_manifest)
        if settings.domain == "knowledge_work":
            _require_gdp_dependencies(settings, domain, manifest)
        digest.update(settings.dependency_manifest.read_bytes())
    else:
        raise ValueError("Freeze dataset/corpus/index/images in dependency_manifest")
    if settings.domain == "knowledge_work":
        digest.update(_image_id(settings.execution_image).encode())
    return digest.hexdigest()


def _image_id(image: str) -> str:
    identity = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        text=True,
        timeout=30,
    ).strip()
    if not identity.startswith("sha256:"):
        raise ValueError("Office image must resolve to an immutable image ID")
    return identity


def _require_gdp_dependencies(
    settings: DomainSettings, domain: Any, manifest: dict[str, Any]
) -> None:
    if settings.hf_cache_dir is None:
        raise ValueError("GDPVal requires an explicitly frozen hf_cache_dir")
    required = [settings.hf_cache_dir]
    for key in ("reference_dir", "meta_prompts_dir"):
        if domain.get(key):
            required.append((settings.domain_config.parent / domain[key]).resolve())
    frozen = [Path(path) for path in manifest["directories"]]
    if any(not any(path.is_relative_to(root) for root in frozen) for path in required):
        raise ValueError(
            "Freeze the GDP cache, references and meta-prompts directories"
        )


def _data_identity(root: Path, config: Any) -> str:
    digest = hashlib.sha256()
    for key in ("dataset_file", "parquet_file", "split_file"):
        if config.get(key):
            path = (root / config[key]).resolve(strict=True)
            digest.update(str(path).encode())
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def grading_identity(settings: DomainSettings, dataset: Path) -> str:
    """构建独立于执行器及产物目录的公共评分身份。"""
    fields = settings.model_dump(
        mode="json",
        exclude={
            "artifact_dir",
            "runner",
            "official_agent_config",
        },
    )
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode())
    digest.update(dataset.read_bytes())
    digest.update(dependency_identity(settings).encode())
    return digest.hexdigest()


def experiment_identity(settings: DomainSettings, dataset: Path) -> str:
    """绑定评分条件和执行器配置，不包含可移动的产物目录。"""
    digest = hashlib.sha256(grading_identity(settings, dataset).encode())
    digest.update(settings.runner.encode())
    if settings.official_agent_config:
        digest.update(
            json.dumps(
                _resolved_config(settings.official_agent_config), sort_keys=True
            ).encode()
        )
    return digest.hexdigest()
