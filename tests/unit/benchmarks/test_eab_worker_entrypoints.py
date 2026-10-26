"""独立 worker 移动后仍能从仓库外启动，且不依赖 Runtime 虚拟环境。"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_domain_worker_help_without_site_packages(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "benchmarks/evoagentbench/domains/domain_worker.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--request" in result.stdout
    assert "--result" in result.stdout


@pytest.mark.parametrize(
    "module",
    [
        "domains.retrieval_feedback",
        "runners.official_baseline_worker",
        "runners.official_tool_transport",
    ],
)
def test_upstream_worker_helpers_need_only_standard_library(
    tmp_path: Path, module: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
            "importlib.import_module(sys.argv[2]); "
            "assert not any(m == 'agentsystem' or m.startswith('agentsystem.') "
            "for m in sys.modules)",
            str(ROOT),
            "benchmarks.evoagentbench." + module,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
