"""Bounded recovery, preferring grading saved execution over a new agent call."""

import asyncio
import json
import time
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, overload
from uuid import uuid4

import yaml

if TYPE_CHECKING:
    from .portable import local_path, write_json
else:
    from portable import local_path, write_json


@overload
def close_scope(
    settings: dict[str, Any], tag: str, value: dict[str, Any]
) -> dict[str, Any]: ...


@overload
def close_scope(settings: dict[str, Any], tag: str, value: None) -> None: ...


def close_scope(
    settings: dict[str, Any], tag: str, value: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Close the actual recovery attempt's public credentials and preserve usage."""
    if not settings.get("research_proxy_scopes"):
        return value
    # Use the maintained project adapter in this process.
    read_scope_usage = import_module(
        "evolution_tasks.evoagentbench_research._internal.research_model_proxy"
    ).read_scope_usage
    revoke_scope = import_module(
        "evolution_tasks.evoagentbench_research._internal.research_model_proxy"
    ).revoke_scope
    # Use the maintained project adapter in this process.
    revoke_redis_scope = import_module(
        "evolution_tasks.evoagentbench_research._internal.research_redis"
    ).revoke_redis_scope
    # Use the maintained project adapter in this process.
    account_usage = import_module(
        "evolution_tasks.evoagentbench_research._internal.research_sandbox"
    ).account_usage

    scopes = Path(settings["research_proxy_scopes"])
    try:
        revoke_scope(scopes, tag)
    finally:
        revoke_redis_scope(tag)
    if value is None:
        return None
    end = time.monotonic() + 2
    usage = read_scope_usage(scopes, tag)
    while usage["pending_requests"] and time.monotonic() < end:
        time.sleep(0.05)
        usage = read_scope_usage(scopes, tag)
    accounted: dict[str, Any] = account_usage(value, usage)
    value = accounted
    return value


def recover(  # noqa: C901 - one owner for deadline, evidence and recovery decisions
    root: Path,
    directory: Path,
    invoke: Callable[..., dict[str, Any]],
    command: list[str],
    request: dict[str, Any],
    kwargs: dict[str, Any],
    *,
    deadline: float,
    stop: Event,
    exhausted: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """All attempts share one clock; successful low scores are terminal results."""
    attempt_number = 0
    while True:
        if stop.is_set():
            raise asyncio.CancelledError
        remaining = deadline - time.time()
        pending = directory / "pending.json"
        if pending.exists():
            previous = json.loads(pending.read_text())
            saved_grade = previous.get("regrade_result")
            if saved_grade:
                result_path = local_path(root, saved_grade)
                if result_path.is_file():
                    saved_value: dict[str, Any] = json.loads(result_path.read_text())
                    if saved_value.get("tracelog", {}).get("verifier") is not None:
                        return close_scope(
                            request["settings"], previous["job_tag"], saved_value
                        )
            execution = local_path(
                root, f".local/evidence/outputs/{previous['job_tag']}/execution.json"
            )
        else:
            execution = None
        call = dict(request)
        current_command = command
        current_kwargs = {**kwargs, "timeout": remaining, "cancel": stop}
        if execution and execution.exists():
            raw: dict[str, Any] = json.loads(execution.read_text())
            if raw["tracelog"]["status"] == "succeeded":
                # Complete output must never be regenerated to improve a verdict.
                if "verifier" in raw["tracelog"]:
                    return close_scope(request["settings"], previous["job_tag"], raw)
                if remaining <= 0:
                    return exhausted()
                workspace = None
                if request["settings"]["domain"] == "knowledge_work":
                    domain = yaml.safe_load(
                        Path(request["settings"]["domain_config"]).read_text()
                    )
                    workspace = (
                        Path(domain["workspace_root"])
                        / previous["job_tag"]
                        / f"{request['ids'][0][:8]}_t1"
                    )
                    if not workspace.is_dir():
                        raise ValueError("Complete output has no original workspace")
                output = directory / f"regrade-{uuid4().hex}"
                output.mkdir()
                write_json(
                    pending,
                    {
                        **previous,
                        "regrade_result": str(
                            (output / "result.json").relative_to(root)
                        ),
                    },
                )
                call = {
                    "settings": request["settings"],
                    "case_id": request["ids"][0],
                    "job_tag": previous["job_tag"],
                    "execution": str(execution),
                    "workspace": str(workspace) if workspace else None,
                    "output": str(output),
                }
                current_command = [
                    command[0],
                    str(Path(__file__).with_name("regrade.py")),
                ]
                current_kwargs.pop("owned_container", None)
            else:
                execution = None
        if not execution or not execution.exists():
            if remaining <= 0:
                return exhausted()
            # Only interrupted/failed execution may restart within its original clock.
            tag = uuid4().hex
            call["job_tag"] = tag
            if request.get("container"):
                prefix = (
                    "eab-office-"
                    if request["settings"]["domain"] == "knowledge_work"
                    else "eab-retrieval-"
                )
                call["container"] = prefix + tag
                current_kwargs["owned_container"] = call["container"]
            write_json(pending, {"job_tag": tag})
        evidence = directory / f"attempt-{uuid4().hex}.json"
        started = time.time()
        tag = previous["job_tag"] if "execution" in call else call["job_tag"]
        try:
            value = None
            try:
                value = invoke(current_command, call, **current_kwargs)
            finally:
                value = close_scope(request["settings"], tag, value)
            write_json(
                evidence,
                {
                    "started": started,
                    "finished": time.time(),
                    "mode": "grade" if "execution" in call else "execute",
                    "status": "succeeded",
                },
            )
            assert value is not None
            return value
        except asyncio.CancelledError:
            raise
        except Exception as error:
            write_json(
                evidence,
                {
                    "started": started,
                    "finished": time.time(),
                    "mode": "grade" if "execution" in call else "execute",
                    "status": "failed",
                    "type": type(error).__name__,
                    "diagnostic": getattr(error, "process_diagnostic", None),
                },
            )
            if time.time() >= deadline:
                # Re-enter once to recover any already archived verdict.
                continue
            attempt_number += 1
            if stop.wait(
                min(60 * 2 ** min(attempt_number - 1, 4), 600, deadline - time.time())
            ):
                raise asyncio.CancelledError from None
