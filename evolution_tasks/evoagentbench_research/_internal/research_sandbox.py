"""Public-only research Runtime sandbox; compatible with upstream Python 3.12."""

import copy
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def public_environment(
    source: Mapping[str, str], *, token: str, proxy_url: str
) -> dict[str, str]:
    """Real model, judge and improver credentials stay in their host owners."""
    return {
        "EAB_MODEL": source["EAB_MODEL"],
        "EAB_BASE_MODEL": source["EAB_MODEL"].removeprefix("openai/"),
        "EAB_API_KEY": token,
        "EAB_BASE_URL": proxy_url,
        "EAB_CANDIDATE_ISOLATED": "1",
        "OTEL_ENDPOINT": source.get("OTEL_ENDPOINT", "http://127.0.0.1:4318/v1/traces"),
        "PYTHONPATH": "/opt/runtime:/opt/runtime-deps",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": "/tmp",
    }


def command(
    *,
    image: str,
    name: str,
    code: Path,
    packages: Path,
    target: Path,
    exchange: Path,
    environment: Mapping[str, str],
    run_label: str = "probe",
) -> list[str]:
    """Mount the maintained public packages, never the project or task root."""
    if not image.startswith("sha256:") or not re.fullmatch(
        r"eab-retrieval-[a-f0-9]{32}", name
    ):
        raise ValueError("Research sandbox requires immutable image and owned name")
    args = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--label",
        "agentsystem.retrieval.run=" + run_label,
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--memory=2g",
        "--cpus=1",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs=/tmp:rw,size=256m,mode=1777",
        "--workdir=/workspace",
    ]
    public_paths = (
        "agentsystem",
        "evolution",
        "benchmarks/evoagentbench",
        "application/evoagentbench_knowledge_agent/plugins",
        "application/evoagentbench_research_agent/plugins",
        "evolution_tasks/evoagentbench_research/_internal",
    )
    for src, dst, readonly in [
        *((code / path, "/opt/runtime/" + path, True) for path in public_paths),
        (packages, "/opt/runtime-deps", True),
        (target, "/candidate", True),
        (exchange, "/workspace", False),
    ]:
        args += [
            "--mount",
            f"type=bind,src={src},dst={dst}" + (",readonly" if readonly else ""),
        ]
    # Values are inherited from the subprocess environment, not exposed in argv.
    for key in environment:
        args += ["--env", key]
    return [
        *args,
        image,
        "python",
        "-m",
        "evolution_tasks.evoagentbench_research._internal.research_network",
        "--request",
        "/workspace/request.json",
        "--result",
        "/workspace/result.json",
    ]


def account_usage(raw: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    """The proxy sees Runtime and plugin requests; never add Runtime usage twice."""
    result = copy.deepcopy(raw)
    metadata, trace = result["metadata"], result["tracelog"]
    observed = usage.get("total_tokens")
    baseline = int(metadata.get("total_tokens", 0))
    if isinstance(observed, int):
        metadata["total_tokens"] = max(baseline, observed)
    trace["model_proxy_usage"] = {
        **usage,
        "runtime_reported_tokens": baseline,
        "tokens_beyond_runtime": max(0, observed - baseline)
        if isinstance(observed, int)
        else None,
    }
    if usage.get("partial"):
        trace["token_accounting"] = "partial"
    return result


def read_public_result(path: Path) -> dict[str, Any]:
    """A candidate-owned pathname must not redirect a privileged host read."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        limit = 64 * 1024 * 1024
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Candidate result must be a bounded regular file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Candidate result exceeded limit")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Candidate result must be an object")
        return result


def run(
    request: dict[str, Any], payload: dict[str, Any], directory: Path
) -> dict[str, Any]:
    """The private lifecycle worker hands off only a public request and target."""
    import yaml

    from evolution_tasks.evoagentbench_research._internal.research_model_proxy import (
        register_scope,
    )
    from evolution_tasks.evoagentbench_research._internal.research_network import (
        host_relays,
    )

    settings = request["settings"]
    scope_dir = Path(settings["research_proxy_scopes"])
    token = register_scope(scope_dir, request["job_tag"])
    environment = public_environment(
        os.environ, token=token, proxy_url=settings["research_proxy_url"]
    )
    environment["EAB_RESEARCH_URL"] = "http://127.0.0.1:19116/mcp"
    environment["EAB_RESEARCH_SESSION_ID"] = request["job_tag"]
    exchange = directory / "public-runtime"
    exchange.mkdir(mode=0o700)
    runtime = yaml.safe_load(Path(payload["runtime_config"]).read_text())
    runtime["asset_store"] = {"type": "local", "root": "assets"}
    runtime["redis"].update(host="127.0.0.1", port=49154)
    from evolution_tasks.evoagentbench_research._internal.research_redis import (
        register_redis_scope,
    )

    runtime["redis"].update(
        username=request["job_tag"], password=register_redis_scope(request["job_tag"])
    )
    runtime["trace"]["enabled"] = False
    runtime["trace"].pop("headers", None)
    (exchange / "runtime.yaml").write_text(yaml.safe_dump(runtime))
    contract = yaml.safe_load(Path(settings["research_model_contract"]).read_text())

    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: environment[m[1]], value
            )
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    public = {
        **payload,
        "domain_environment": {"kind": "research", "url": "http://127.0.0.1:19116/mcp"},
        "target": "/candidate",
        "runtime_config": "/workspace/runtime.yaml",
        "image_archive": None,
        "candidate_plugins_enabled": True,
        "model_contract": resolve(contract),
    }
    (exchange / "request.json").write_text(json.dumps(public))
    args = command(
        image=settings["research_execution_image"],
        name=request["container"],
        code=Path(settings["research_runtime_source"]),
        packages=Path(settings["research_site_packages"]),
        target=Path(request["target"]),
        exchange=exchange,
        environment=environment,
        run_label=str(Path(settings["artifact_dir"]).parent),
    )
    with host_relays(exchange / "research-sockets"):
        subprocess.run(
            args,
            check=True,
            timeout=settings["case_timeout_seconds"] + 30,
            env={**os.environ, **environment},
        )
    raw = read_public_result(exchange / "result.json")
    return raw
