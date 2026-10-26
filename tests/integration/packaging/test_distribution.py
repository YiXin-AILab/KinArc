"""Python 构建、console script 与 Docker 启动契约测试。"""

import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile


def test_built_wheel_contains_evolution_agent_assets(tmp_path: Path) -> None:
    """发布包不得遗漏 Evolution Agent 的配置和 Prompt。"""
    source = tmp_path / "source"
    source.mkdir()
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for encoded in tracked:
        if not encoded:
            continue
        relative = Path(encoded.decode())
        if not relative.is_file():
            continue
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(relative, destination)
    output = tmp_path / "wheel"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        check=True,
        capture_output=True,
        text=True,
        cwd=source,
    )
    wheels = tuple(output.glob("*.whl"))
    assert len(wheels) == 1

    with ZipFile(wheels[0]) as wheel:
        members = set(wheel.namelist())

    assert "evolution/agent/config/agent.yaml" in members
    assert "evolution/agent/prompt/system.md" in members
    removed_prefixes = (
        "evolution/domain/",
        "evolution/evolution_agent/",
        "evolution/examples/",
        "evolution/improver/",
        "evolution/orchestration/",
        "evolution/repository/",
    )
    assert not any(
        member.startswith(prefix) for prefix in removed_prefixes for member in members
    )
    assert "evolution/scenario_loader.py" not in members


def test_docker_build_context_excludes_local_and_generated_files() -> None:
    """Docker 构建不得上传虚拟环境、Git 数据和本地产物。"""
    patterns = set(Path(".dockerignore").read_text(encoding="utf-8").splitlines())

    for required in (".git", ".venv", "build", "dist", "logs", "*.egg-info"):
        assert required in patterns
