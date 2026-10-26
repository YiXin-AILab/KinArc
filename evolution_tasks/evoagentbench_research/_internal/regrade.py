"""Grade retained output with the frozen grader; never execute an agent."""

import argparse
import json
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).parent))


def grade(request: dict[str, Any]) -> dict[str, Any]:
    # Use the maintained project adapter in this process.
    load_adapter = import_module(
        "benchmarks.evoagentbench.domains.domain_worker"
    ).load_adapter
    normalized_verdict = import_module(
        "benchmarks.evoagentbench.domains.domain_worker"
    ).normalized_verdict
    if TYPE_CHECKING:
        from .portable import file_inventory, write_json
    else:
        from portable import file_inventory, write_json

    settings = request["settings"]
    original = Path(request["execution"])
    result: dict[str, Any] = json.loads(original.read_text())
    if result["tracelog"]["status"] != "succeeded":
        raise ValueError("Only complete executions may be regraded")
    adapter = load_adapter(settings)
    tasks = adapter.load_tasks(SimpleNamespace(split=None, task=request["case_id"]))
    if len(tasks) != 1:
        raise ValueError("Regrade must bind exactly one case")
    workspace = Path(request["workspace"]) if request.get("workspace") else None
    before = file_inventory(workspace) if workspace else []
    env = {"workspace_dir": str(workspace)} if workspace else {}
    output = Path(request["output"]) / "official"
    if settings["domain"] == "information_retrieval":
        # Use the maintained project adapter in this process.
        verify_response = import_module(
            "benchmarks.evoagentbench.domains.domain_worker"
        ).verify_response

        raw = verify_response(adapter, tasks[0], env, output, request, result["result"])
    else:
        raw = adapter.verify(
            tasks[0], env, output, agent_result={"response": result["result"]}
        )
    if workspace and file_inventory(workspace) != before:
        raise ValueError("Grader changed the original workspace")
    write_json(Path(request["output"]) / "verdict.private.json", raw)
    result["tracelog"]["verifier"] = normalized_verdict(raw, adapter.pass_threshold())
    result["tracelog"]["regraded_from"] = str(original)
    return result


def main() -> None:
    if TYPE_CHECKING:
        from .portable import write_json
    else:
        from portable import write_json

    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    result = grade(request)
    write_json(Path(request["output"]) / "result.json", result)
    write_json(args.result, result)


if __name__ == "__main__":
    main()
