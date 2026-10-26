"""Task worker using the current project in a cancellation-isolated process."""

import argparse
import json
import os
import signal
import sys
import traceback
from importlib import import_module
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).parent))


def main() -> None:
    """Dispatch a dataset using current project code and task-local resources."""
    if TYPE_CHECKING:
        from .portable import write_json
    else:
        from portable import write_json
    if TYPE_CHECKING:
        from .prepare import prepare
    else:
        from prepare import prepare

    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    root = args.task_dir.resolve(strict=True)
    service_environment = root / ".local/services-env.json"
    if service_environment.exists():
        os.environ.update(json.loads(service_environment.read_text()))
    request = json.loads(args.request.read_text())
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        settings = prepare(root)
        # Use the maintained project adapter in this process.
        DomainEvaluator = import_module(
            "benchmarks.evoagentbench.domains.domain_adapter"
        ).DomainEvaluator
        DomainExecutor = import_module(
            "benchmarks.evoagentbench.domains.domain_adapter"
        ).DomainExecutor
        # Use the maintained project adapter in this process.
        settings_for = import_module(
            "benchmarks.evoagentbench.domains.domain_settings"
        ).settings_for
        from evolution.models import DatasetRole, ExecutionResult

        resolved = settings_for(root, settings)
        dataset = Path(request["dataset"])
        role = DatasetRole(request["role"])
        if request["operation"] == "execute":
            if TYPE_CHECKING:
                from .deadlines import install
            else:
                from deadlines import install
            if TYPE_CHECKING:
                from .diagnostics import install as install_diagnostics
            else:
                from diagnostics import install as install_diagnostics

            install_diagnostics(root)
            install(root, stop)
            result = DomainExecutor(resolved).execute(
                Path(request["target"]),
                dataset,
                role,
                max_case_concurrency=request["concurrency"],
                max_retries=0,
                cancel_event=stop,
            )
        elif request["operation"] == "evaluate":
            result = DomainEvaluator(resolved).evaluate(
                ExecutionResult.model_validate(request["execution"]),
                dataset,
                role,
            )
        else:
            raise ValueError("Unsupported worker operation")
        write_json(args.result, result.model_dump(mode="json"))
    except BaseException as error:
        # Public diagnostics contain only types and source locations, never
        # provider response bodies, endpoint credentials or request payloads.
        write_json(
            root / ".local/last-worker-error.json",
            {
                "type": type(error).__name__,
                "frames": [
                    {
                        "file": Path(f.filename).name,
                        "line": f.lineno,
                        "function": f.name,
                    }
                    for f in traceback.extract_tb(error.__traceback__)
                ],
            },
        )
        raise


if __name__ == "__main__":
    main()
