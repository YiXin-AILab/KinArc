"""四组冻结结果应可复现，提示词组只能修改提示词。"""

import json
from pathlib import Path

import pytest
from benchmarks.evoagentbench.experiments.arms import freeze_arms, verify_arms


def assets(root: Path, prompt: str, memory: str) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "prompt").mkdir()
    (root / "memory").mkdir()
    (root / "config/agent.yaml").write_text("prompt: prompt/system.md\n")
    (root / "prompt/system.md").write_text(prompt)
    (root / "memory/index.md").write_text(memory)
    return root


def test_prompt_transplant_and_mutation_detection(tmp_path: Path) -> None:
    seed = assets(tmp_path / "seed", "original", "old")
    best = assets(tmp_path / "best", "read skills/new.md", "new")
    destination = tmp_path / "arms"
    freeze_arms(seed, best, destination)
    assert (destination / "prompt/memory/index.md").read_text() == "old"
    assert (destination / "prompt/prompt/system.md").read_text() == "read skills/new.md"
    assert (destination / "evolved/memory/index.md").read_text() == "new"
    verify_arms(destination)
    (destination / "yx/memory/index.md").write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        verify_arms(destination)


def test_freeze_refuses_config_changes_and_overwrite(tmp_path: Path) -> None:
    seed = assets(tmp_path / "seed", "p", "m")
    best = assets(tmp_path / "best", "q", "n")
    (best / "config/agent.yaml").write_text("prompt: prompt/other.md\n")
    with pytest.raises(ValueError, match="config"):
        freeze_arms(seed, best, tmp_path / "arms")
    assert not (tmp_path / "arms").exists()
    freeze_arms(seed, seed, tmp_path / "arms")
    assert (
        json.loads((tmp_path / "arms/manifest.json").read_text())["prompt_dependencies"]
        == []
    )
    with pytest.raises(FileExistsError):
        freeze_arms(seed, seed, tmp_path / "arms")
