"""按摘要冻结实验组，并精确执行提示词移植。"""

import json
import re
import shutil
import tempfile
from pathlib import Path

import yaml
from evolution.candidates import tree_digest


def copy_assets(source: Path, destination: Path) -> None:
    """仅复制真实文件，不通过候选符号链接读取宿主数据。"""
    if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Candidate assets must not contain symlinks")
    shutil.copytree(source, destination)
    for path in destination.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    destination.chmod(0o700)


def prompt_path(target: Path) -> Path:
    """读取配置中的提示词路径，不解析模型凭证。"""
    config = yaml.safe_load((target / "config/agent.yaml").read_text())
    relative = Path(config["prompt"])
    path = (target / relative).resolve(strict=True)
    if relative.is_absolute() or not path.is_relative_to(target.resolve()):
        raise ValueError("Prompt must be inside target")
    return path


def freeze_arms(seed: Path, best: Path, destination: Path) -> None:
    """冻结 B、D 组，仅替换 B 组提示词以构建 C 组。"""
    if destination.exists():
        raise FileExistsError("Arms already frozen; use a new experiment")
    if tree_digest(seed / "config") != tree_digest(best / "config"):
        raise ValueError("Evolution changed the frozen agent config")
    seed_prompt, best_prompt = prompt_path(seed), prompt_path(best)
    relative = seed_prompt.relative_to(seed.resolve())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".arms-") as tmp:
        stage = Path(tmp) / "arms"
        stage.mkdir()
        for arm, source in (("yx", seed), ("prompt", seed), ("evolved", best)):
            copy_assets(source, stage / arm)
        (stage / "prompt" / relative).write_bytes(best_prompt.read_bytes())
        missing = sorted(
            {
                reference.rstrip(".,;:")
                for reference in re.findall(
                    r"(?:skills|memory)/[\w./-]+", best_prompt.read_text()
                )
                if not (stage / "prompt" / reference.rstrip(".,;:")).exists()
            }
        )
        manifest = {
            "arms": {
                arm: tree_digest(stage / arm) for arm in ("yx", "prompt", "evolved")
            },
            "seed_sha256": tree_digest(seed),
            "best_sha256": tree_digest(best),
            "prompt_file": str(relative),
            "prompt_dependencies": missing,
            "dependency_check": "literal paths only; no automatic repair",
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2))
        stage.rename(destination)


def verify_arms(directory: Path) -> dict[str, str]:
    """执行或发布前拒绝实验组内容发生变化。"""
    manifest = json.loads((directory / "manifest.json").read_text())
    hashes: dict[str, str] = manifest["arms"]
    if set(hashes) != {"yx", "prompt", "evolved"}:
        raise ValueError("Incomplete frozen arm manifest")
    for arm, digest in hashes.items():
        if tree_digest(directory / arm) != digest:
            raise ValueError(f"Frozen {arm} arm changed")
    return hashes
