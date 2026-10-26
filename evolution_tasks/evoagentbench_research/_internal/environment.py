"""Verify installed interpreter and distributions against the exported pins."""

import json
import re
import subprocess
from pathlib import Path


def verify_environment(root: Path) -> None:
    """Verify third-party grader pins and the current project container install."""
    require_installed_files(root)
    for name in ("upstream",):
        python = root / "dependencies" / name / ".venv/bin/python"
        version = (root / "configs" / f"{name}-python.txt").read_text().strip()
        code = (
            "import json,sys,importlib.metadata as m;"
            "print(json.dumps({'python':'.'.join(map(str,sys.version_info[:3])),"
            "'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"
        )
        result = subprocess.run(
            [str(python), "-I", "-c", code],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        actual = json.loads(result.stdout)
        if actual["python"] != version:
            raise ValueError(f"{name}: Python version changed")
        installed = {
            re.sub(r"[-_.]+", "-", k).lower(): v for k, v in actual["packages"].items()
        }
        for line in (
            (root / "configs" / f"{name}-requirements.txt").read_text().splitlines()
        ):
            package, expected = line.split("==", 1)
            key = re.sub(r"[-_.]+", "-", package).lower()
            if installed.get(key) != expected:
                raise ValueError(f"{name}: missing or changed distribution {package}")

    settings = json.loads((root / "configs/settings.json").read_text())
    if settings["domain"] == "information_retrieval":
        from benchmarks.evoagentbench.common.identity import file_digest, project_root

        receipt = json.loads((root / ".local/container-install.json").read_text())
        if receipt["project_lock_sha256"] != file_digest(project_root() / "uv.lock"):
            raise ValueError("Current project lock changed; rerun task install")
        if receipt["image"] != settings["research_execution_image"]:
            raise ValueError("Research container image changed; rerun task install")
        if not (root / ".local/container-python").is_dir():
            raise FileNotFoundError("Install current project container dependencies")


def require_installed_files(root: Path) -> None:
    """Report all absent installation products before invoking an interpreter."""
    required = ["dependencies/upstream/.venv/bin/python"]
    settings = json.loads((root / "configs/settings.json").read_text())
    if settings["domain"] == "information_retrieval":
        required.extend([".local/container-install.json", ".local/container-python"])
    missing = [path for path in required if not (root / path).exists()]
    if missing:
        raise FileNotFoundError("Missing installed dependencies: " + ", ".join(missing))
