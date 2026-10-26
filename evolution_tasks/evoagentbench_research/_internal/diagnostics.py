"""Retain failed worker logs privately without exposing provider payloads."""

import shutil
from importlib import import_module
from pathlib import Path
from typing import IO, Any
from uuid import uuid4


def install(root: Path) -> None:
    # Use the maintained project adapter in this process.
    process: Any = import_module("benchmarks.evoagentbench.common.process")

    original = process._save_diagnostic

    def save(error: Any, status: str, stream: IO[Any]) -> None:
        destination = root / ".local/process-diagnostics" / f"{uuid4().hex}.log"
        destination.parent.mkdir(parents=True, exist_ok=True)
        stream.flush()
        shutil.copyfile(stream.name, destination)
        destination.chmod(0o600)
        original(error, status, stream)
        error.process_diagnostic["private_log"] = str(destination)

    process._save_diagnostic = save
