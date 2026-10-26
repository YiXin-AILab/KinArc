"""Exercise only run.sh bootstrap against a local Git repository, without an eval."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks.tau2bench.config import IntegrationConfig

ROOT = Path(__file__).resolve().parents[4]


def git(*args: str, cwd: Path) -> str:
    """Run fixture Git commands without network access."""
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture(scope="module")
def bootstrap_source(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """为五种独立 checkout 场景共用只读上游仓库和补丁。"""
    source = tmp_path_factory.mktemp("tau2-bootstrap") / "upstream"
    source.mkdir()
    git("init", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    (source / "sample").write_text("before\n")
    git("add", "sample", cwd=source)
    git("commit", "-m", "initial", cwd=source)
    git("tag", "v1.0.1", cwd=source)
    (source / "sample").write_text("after\n")
    return source, git("diff", cwd=source) + "\n"


@pytest.mark.parametrize("state", ["missing", "clean", "applied", "conflict", "wrong"])
def test_bootstrap_is_idempotent_and_rejects_conflicts(
    tmp_path: Path, bootstrap_source: tuple[Path, str], state: str
) -> None:
    """Clone/apply once; preserve incompatible existing checkouts."""
    source, patch_content = bootstrap_source
    script_dir = tmp_path / "benchmark"
    (script_dir / "patches").mkdir(parents=True)
    patch = script_dir / "patches/native-banking.patch"
    patch.write_text(patch_content)
    checkout = script_dir / "tau2-bench"
    if state != "missing":
        git("clone", str(source), str(checkout), cwd=tmp_path)
        if state == "applied":
            git("apply", str(patch), cwd=checkout)
        elif state == "conflict":
            (checkout / "sample").write_text("user changes\n")
        elif state == "wrong":
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "different revision",
                cwd=checkout,
            )

    script = (ROOT / "benchmarks/tau2bench/run.sh").read_text()
    bootstrap = script.split("# TAU2 安装准备开始\n")[1].split("# TAU2 安装准备结束")[0]
    bootstrap = bootstrap.replace(
        "https://github.com/sierra-research/tau2-bench.git", str(source)
    )
    runner = tmp_path / "bootstrap.sh"
    runner.write_text('set -euo pipefail\nSCRIPT_DIR="$1"\n' + bootstrap)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_binary = shutil.which("git")
    assert git_binary is not None
    for command in ("git", "uv"):
        wrapper = bin_dir / command
        wrapper.write_text(
            f'#!/bin/bash\necho "{command} $*" >> "$CALL_LOG"\n'
            + (f'exec {shlex.quote(git_binary)} "$@"\n' if command == "git" else "")
        )
        wrapper.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALL_LOG": str(calls),
    }
    result = subprocess.run(
        ["bash", str(runner), str(script_dir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if state in {"conflict", "wrong"}:
        assert result.returncode != 0
        assert "uv sync" not in calls.read_text()
        expected = "user changes\n" if state == "conflict" else "before\n"
        assert (checkout / "sample").read_text() == expected
        return
    assert result.returncode == 0, result.stderr
    assert (checkout / "sample").read_text() == "after\n"
    calls.write_text("")
    subprocess.run(
        ["bash", str(runner), str(script_dir)], env=env, check=True, timeout=30
    )
    lines = calls.read_text().splitlines()
    assert not any("clone " in line for line in lines)
    assert not any(" apply " in line and "--check" not in line for line in lines)
    assert any("uv sync" in line for line in lines)


@pytest.mark.parametrize(
    ("agent", "exit_code", "timeout_override"),
    [
        ("banking_agent", 0, None),
        ("banking_golden_agent", 7, "12.5"),
    ],
)
def test_run_script_forwards_native_arguments(
    tmp_path: Path, agent: str, exit_code: int, timeout_override: str | None
) -> None:
    """验证默认值、覆盖值和退出码，不重复运行独立参数的笛卡尔积。"""
    text = (ROOT / "benchmarks/tau2bench/run.sh").read_text()
    tail = text[text.index("# 用户追加的原生 CLI 参数") :]
    runner = tmp_path / "run.sh"
    runner.write_text(
        'set -euo pipefail\nSCRIPT_DIR="$1"\nREPO_ROOT="$2"\nshift 2\n' + tail
    )
    binary = tmp_path / "uv"
    binary.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "from pathlib import Path\n"
        "from benchmarks.tau2bench.config import load_config\n"
        "Path(os.environ['CAPTURE']).write_text(json.dumps({"
        "'args': sys.argv[1:], 'config': load_config().model_dump(mode='json')}))\n"
        f"sys.exit({exit_code})\n"
    )
    binary.chmod(0o755)
    capture = tmp_path / "arguments.json"
    arguments = [
        "--retrieval-config",
        "full_kb",
        "--num-trials",
        "2",
        "--max-concurrency",
        "3",
        "--task-ids",
        "task_001",
        "task_002",
        "--agent-llm-args",
        '{"top_p": 0.8}',
    ]
    result = subprocess.run(
        ["bash", str(runner), str(tmp_path), str(ROOT), *arguments],
        env={
            "PATH": f"{tmp_path}:{os.defpath}",
            "CAPTURE": str(capture),
            "TAU2_RUNTIME_PYTHON": sys.executable,
            "TAU2_AGENT_DIR": f"agents/{agent}",
            **(
                {
                    f"TAU2_{name.upper()}": timeout_override
                    for name in IntegrationConfig.model_fields
                    if name.endswith("_timeout_seconds")
                }
                if timeout_override is not None
                else {}
            ),
        },
        timeout=10,
    )
    assert result.returncode == exit_code
    captured = json.loads(capture.read_text())
    actual = captured["args"]
    assert actual[-len(arguments) :] == arguments
    assert "--agent-llm" not in actual
    assert captured["config"]["agent_dir"] == str(
        ROOT / "benchmarks/tau2bench/agents" / agent
    )
    for name, field in IntegrationConfig.model_fields.items():
        if name.endswith("_timeout_seconds"):
            expected = (
                float(timeout_override)
                if timeout_override is not None
                else field.default
            )
            assert captured["config"][name] == expected
