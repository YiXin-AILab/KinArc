"""Private JSON RPC to the current project worker, with cooperative cancellation."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event
from typing import Any

from benchmarks.evoagentbench.common.identity import project_root


def invoke(
    root: Path, payload: dict[str, Any], timeout: float, cancel: Event | None = None
) -> dict[str, Any]:
    """Supervise one batch; SIGTERM gives its workers time to clean up containers."""
    if cancel is not None and cancel.is_set():
        raise asyncio.CancelledError
    python = sys.executable
    exchange = root / ".local/exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=exchange) as directory:
        request = Path(directory) / "request.json"
        result = Path(directory) / "result.json"
        request.write_text(json.dumps(payload), encoding="utf-8")
        request.chmod(0o600)
        # Logs may contain provider exception details. Keep private and do not
        # include their text in public errors or version-controlled artifacts.
        with tempfile.NamedTemporaryFile(
            dir=root / ".local", suffix=".worker.log"
        ) as log:
            child = subprocess.Popen(
                [
                    str(python),
                    "-B",
                    str(Path(__file__).with_name("runner.py")),
                    "--task-dir",
                    str(root),
                    "--request",
                    str(request),
                    "--result",
                    str(result),
                ],
                cwd=root,
                stdout=log,
                stderr=log,
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    # The grader launches runtime_worker with python -m. Its
                    # descendants must resolve the same maintained project tree.
                    "PYTHONPATH": str(project_root()),
                },
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout
                while child.poll() is None:
                    if cancel is not None and cancel.is_set():
                        raise asyncio.CancelledError
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Portable worker exceeded batch deadline")
                    time.sleep(0.1)
                if child.returncode != 0:
                    raise RuntimeError(
                        f"Portable worker failed (exit {child.returncode}); "
                        "inspect .local/last-worker-error.json"
                    )
                value = json.loads(result.read_text())
                if not isinstance(value, dict):
                    raise ValueError("Worker response must be an object")
                return value
            finally:
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
                    try:
                        child.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=10)
                        raise RuntimeError(
                            "Worker failed cooperative cleanup; inspect its containers "
                            "before another run"
                        ) from None
