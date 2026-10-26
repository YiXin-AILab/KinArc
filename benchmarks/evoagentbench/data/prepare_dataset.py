"""从指定版本的官方划分中创建仅含公开信息的任务清单。"""

import argparse
import hashlib
import json
import random
from pathlib import Path

from agentsystem.config.loader import load_yaml_config
from evolution.task import TaskConfig

from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.domains.domain_adapter import upstream
from benchmarks.evoagentbench.domains.domain_settings import settings_for


def split_ids(
    raw: dict[str, list[str]], validation_count: int, seed: int
) -> dict[str, list[str]]:
    """仅从官方训练集划分验证集，保留测试集案例不变。"""
    train, test = raw["train"], raw["test"]
    for values in (train, test):
        if not values or len(values) != len(set(values)):
            raise ValueError("Official split must contain unique, nonempty IDs")
    if set(train) & set(test) or not 0 < validation_count < len(train):
        raise ValueError("Overlapping split or invalid validation size")
    shuffled = sorted(train)
    random.Random(seed).shuffle(shuffled)
    validation = set(shuffled[:validation_count])
    return {
        "train": [value for value in train if value not in validation],
        "validation": [value for value in train if value in validation],
        "test": list(test),
    }


def main() -> None:
    """一次性准备文件，拒绝覆盖已有实验的数据集。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--validation-count", type=int, default=36)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--preset", choices=("formal", "pilot"), default="formal")
    args = parser.parse_args()
    prepare(args.task_dir.resolve(), args.validation_count, args.seed, args.preset)


def pilot_ids(
    raw: dict[str, list[str]], validation_count: int, seed: int
) -> dict[str, list[str]]:
    """从正式训练集抽取试运行留出集，不使用官方测试集或验证集。"""
    formal = split_ids(raw, validation_count, seed)
    pool = sorted(formal["train"])
    if len(pool) < 40:
        raise ValueError("Pilot requires at least 40 formal training cases")
    random.Random(seed).shuffle(pool)
    return {"train": pool[:20], "validation": pool[20:28], "test": pool[28:40]}


def prepare(root: Path, validation_count: int, seed: int, preset: str) -> None:
    """导出一套冻结且仅含公开信息的实验数据集。"""
    config = load_yaml_config(root / "config.yaml", TaskConfig)
    settings = settings_for(root, config.settings)
    split = settings.upstream_repo / "benchmark/data/splits" / f"{settings.domain}.json"
    content = split.read_bytes()
    splitter = pilot_ids if preset == "pilot" else split_ids
    roles = splitter(json.loads(content), validation_count, seed)
    paths = {role: root / "datasets" / f"{role}.json" for role in roles}
    if any(path.exists() for path in paths.values()):
        raise FileExistsError(
            "Prepared datasets already exist; use a new task directory"
        )
    prepared = {}
    for role, ids in roles.items():
        exported = upstream(settings, "prepare", ids)
        rows = exported["cases"]
        if not isinstance(rows, list):
            raise ValueError("Upstream must export a case array")
        cases = tuple(Case.model_validate(case) for case in rows)
        if [case.id for case in cases] != ids:
            raise ValueError("Exported task membership/order differs")
        prepared[role] = Dataset(
            role=role,
            upstream_revision=settings.upstream_revision,
            source_sha256=hashlib.sha256(content).hexdigest(),
            cases=cases,
        )
    for role, dataset in prepared.items():
        paths[role].parent.mkdir(parents=True, exist_ok=True)
        paths[role].write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    (root / "datasets/split_manifest.json").write_text(
        json.dumps(
            {
                "preset": preset,
                "seed": seed,
                "validation_count": validation_count,
                "source_sha256": hashlib.sha256(content).hexdigest(),
                "test_source": "official_train"
                if preset == "pilot"
                else "official_test",
                "roles": roles,
            },
            indent=2,
        )
    )
    print(f"Prepared train/validation/test: {[len(roles[r]) for r in roles]}")


if __name__ == "__main__":
    main()
