"""按角色与任务 ID 的显式互斥映射清单导出公开领域案例。"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from agentsystem.config.loader import load_yaml_config
from evolution.task import TaskConfig

from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.domains.domain_adapter import upstream
from benchmarks.evoagentbench.domains.domain_settings import settings_for


def prepare(root: Path, manifest: Path) -> None:
    """写入任意数据集前，先准备并校验全部角色的数据。"""
    config = load_yaml_config(root / "config.yaml", TaskConfig)
    settings = settings_for(root, config.settings)
    content = manifest.read_bytes()
    roles = json.loads(content)
    if set(roles) != {"train", "validation", "test"}:
        raise ValueError("Manifest requires exactly train/validation/test")
    seen: set[str] = set()
    prepared = {}
    names: tuple[Literal["train", "validation", "test"], ...] = (
        "train",
        "validation",
        "test",
    )
    for role in names:
        ids = roles[role]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(key, str) or not key for key in ids)
            or len(set(ids)) != len(ids)
            or seen.intersection(ids)
        ):
            raise ValueError("Role IDs must be nonempty, unique, disjoint strings")
        seen.update(ids)
        destination = root / "datasets" / f"{role}.json"
        if destination.exists():
            raise FileExistsError(destination)
        exported = upstream(settings, "prepare", ids)
        rows = exported.get("cases")
        if not isinstance(rows, list):
            raise ValueError("Worker did not export public cases")
        cases = tuple(Case.model_validate(row) for row in rows)
        if [case.id for case in cases] != ids:
            raise ValueError("Exported task membership/order differs")
        prepared[destination] = Dataset(
            role=role,
            upstream_revision=settings.upstream_revision,
            source_sha256=hashlib.sha256(content).hexdigest(),
            cases=cases,
        )
    for destination, dataset in prepared.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x") as output:
            output.write(dataset.model_dump_json(indent=2))


def main() -> None:
    """根据 CLI 指定的任务路径与 ID 清单路径导出公开数据集。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--ids", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.task_dir.resolve(), args.ids)


if __name__ == "__main__":
    main()
