"""Cumulative per-target/case deadlines, retaining success and failure evidence."""

import fcntl
import hashlib
import json
import time
from importlib import import_module
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from .portable import write_json
else:
    from portable import write_json

LIMIT = 1200


def zero(ledger: Path) -> dict[str, Any]:
    """Record a budget exhaustion, explicitly distinct from an official verdict."""
    return {
        "result": "",
        "metadata": {"total_tokens": 0, "duration_seconds": LIMIT},
        "tracelog": {
            "status": "succeeded",
            "execution_status": "user_deadline_exceeded",
            "usage_missing": True,
            "token_accounting": "missing",
            "deadline_seconds": LIMIT,
            "deadline_evidence": str(ledger),
            "retry_allowed": False,
            "verifier": {
                "score": 0.0,
                "passed": False,
                "pass_threshold": 0.0,
                "method": "user_requested_20min_failure",
                "official_verdict": False,
            },
        },
    }


def install(root: Path, stop: Event) -> None:
    """Wrap the task process owner without changing valid scores or sampling."""
    from evolution.candidates import tree_digest

    # This task process imports the maintained project adapter.
    domain_adapter: Any = import_module(
        "benchmarks.evoagentbench.domains.domain_adapter"
    )

    original = domain_adapter.invoke
    protocol = json.loads((root / ".local/relocation.json").read_text())["protocol_id"]

    def invoke(command: list[str], request: dict[str, Any], **kwargs: Any) -> Any:
        if request.get("operation") != "trial":
            return original(command, request, **kwargs)
        if len(request["ids"]) != 1:
            raise ValueError("A deadline must bind exactly one case")
        case = request["ids"][0]
        digest = tree_digest(Path(request["target"]))
        key = hashlib.sha256(f"{protocol}\0{digest}\0{case}".encode()).hexdigest()
        directory = root / ".local/deadlines" / key
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "owner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = directory / "result.json"
            if receipt.exists():
                return json.loads(receipt.read_text())
            ledger = directory / "budget.json"
            if not ledger.exists():
                write_json(
                    ledger,
                    {
                        "case_id": case,
                        "target_sha256": digest,
                        "protocol": protocol,
                        "deadline_at": time.time() + LIMIT,
                        "limit_seconds": LIMIT,
                    },
                )
            deadline = json.loads(ledger.read_text())["deadline_at"]
            remaining = max(0, min(LIMIT, deadline - time.time()))
            attempt = directory / f"{uuid4().hex}.json"
            facts = {
                "remaining_seconds": remaining,
                "started_at": time.time(),
                "job_tag": request["job_tag"],
            }
            write_json(attempt, facts)
            kwargs["timeout"] = min(kwargs["timeout"], remaining)
            kwargs["cancel"] = stop
            if TYPE_CHECKING:
                from .recovery import recover
            else:
                from recovery import recover

            value = recover(
                root,
                directory,
                original,
                command,
                request,
                kwargs,
                deadline=deadline,
                stop=stop,
                exhausted=lambda: zero(ledger),
            )
            write_json(receipt, value)
            write_json(
                attempt, {**facts, "finished_at": time.time(), "receipt": "result.json"}
            )
            return value

    domain_adapter.invoke = invoke
