"""用于外部依赖与试验配对的可复现身份。"""

import hashlib
import json
from pathlib import Path


def file_digest(path: Path) -> str:
    """对大型缓存数据集计算摘要，不将其内容复制到报告中。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def project_root() -> Path:
    """Return the single maintained project used by host and worker processes."""
    return Path(__file__).resolve().parents[3]


def source_digest() -> str:
    """绑定当前源码、运行资源和依赖锁，包括未提交的改动。"""
    root = project_root()
    sources = {
        str(path.relative_to(root)): file_digest(path)
        for directory in (
            "agentsystem",
            "evolution",
            "benchmarks/evoagentbench",
            "application/evoagentbench_knowledge_agent",
            "application/evoagentbench_research_agent",
            "evolution_tasks/evoagentbench_knowledge/_internal",
            "evolution_tasks/evoagentbench_research/_internal",
        )
        for path in (root / directory).rglob("*")
        if path.is_file()
        and not any(
            part.startswith(".") or part == "__pycache__"
            for part in path.relative_to(root).parts
        )
        and path.suffix != ".pyc"
    }
    for name in ("uv.lock", "pyproject.toml"):
        if (root / name).is_file():
            sources[name] = file_digest(root / name)
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()


def bind_manifest(path: Path, identity: str) -> None:
    """禁止在不同协议条件下复用缓存的进化证据。"""
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(identity + "\n")
    except FileExistsError:
        if path.read_text().strip() != identity:
            raise ValueError(
                "Experiment protocol changed; use a new task directory"
            ) from None
