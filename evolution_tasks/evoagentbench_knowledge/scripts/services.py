"""Own foreground Redis/search/model services with task-scoped cleanup."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.evoagentbench.common.identity import project_root

TEMPLATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE / "_internal"))
if TYPE_CHECKING:
    from .._internal.portable import write_json
else:
    from portable import write_json
if TYPE_CHECKING:
    from .._internal.service_docker import service_environment
else:
    from service_docker import service_environment


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:  # noqa: C901 - one lifecycle owner for all task services
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path)
    args = parser.parse_args()
    root = args.task_dir.resolve(strict=True)
    if TYPE_CHECKING:
        from .._internal.environment import verify_environment
        from .._internal.portable import require_current_project, verify_files
    else:
        from environment import verify_environment
        from portable import require_current_project, verify_files
    manifest = json.loads((root / "manifests/resources.json").read_text())
    require_current_project(root, manifest)
    verify_files(root, manifest["files"], check_hashes=False)
    verify_environment(root)
    local = root / ".local"
    local.mkdir(exist_ok=True)
    lock = (local / "services.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    settings = json.loads((root / "configs/settings.json").read_text())
    service_config = json.loads((root / "configs/services.json").read_text())
    redis_image = service_config["redis_image"]
    docker_env = service_environment()
    if not redis_image.startswith("sha256:"):
        raise ValueError("Public Redis image must be content-pinned")
    subprocess.run(
        ["docker", "image", "inspect", redis_image],
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=30,
        env=docker_env,
    )
    environment = dict(os.environ)
    # Persist transport identity across restarts so receipt/deadline keys do not
    # change merely because a service was stopped and restarted.
    saved_environment = local / "services-env.json"
    previous = (
        json.loads(saved_environment.read_text()) if saved_environment.exists() else {}
    )
    children = []
    containers = []
    streams = []
    stopping = False
    suffix = hashlib.sha256(str(root).encode()).hexdigest()[:12]

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for role in ("parent", "public"):
            if role == "public" and settings["domain"] != "information_retrieval":
                continue
            port_key = "EAB_REDIS_PORT" if role == "parent" else "EAB_PUBLIC_REDIS_PORT"
            password_key = (
                "EAB_REDIS_PASSWORD"
                if role == "parent"
                else "EAB_RESEARCH_REDIS_PASSWORD"
            )
            port = int(previous[port_key]) if port_key in previous else free_port()
            password = previous.get(password_key) or secrets.token_urlsafe(32)
            config = local / f"redis-{role}.conf"
            config.write_text(
                f"bind 0.0.0.0\nport 6379\nrequirepass {password}\n"
                'save ""\nappendonly no\n'
            )
            config.chmod(0o600)
            name = f"eab-portable-{suffix}-{role}"
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--label",
                    f"eab.portable.owner={suffix}",
                    "--publish",
                    f"127.0.0.1:{port}:6379",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--tmpfs",
                    "/data:rw,mode=1777",
                    "--mount",
                    f"type=bind,src={config},dst=/run/eab.conf,readonly",
                    redis_image,
                    "redis-server",
                    "/run/eab.conf",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=60,
                env=docker_env,
            )
            containers.append(name)
            if role == "parent":
                environment.update(
                    EAB_REDIS_HOST="127.0.0.1",
                    EAB_REDIS_PORT=str(port),
                    EAB_REDIS_USERNAME="default",
                    EAB_REDIS_PASSWORD=password,
                )
            else:
                environment.update(
                    EAB_PUBLIC_REDIS_PORT=str(port),
                    EAB_RESEARCH_REDIS_PASSWORD=password,
                )
        if settings["domain"] == "information_retrieval":
            for key in ("EAB_PUBLIC_MCP_PORT", "EAB_PUBLIC_MODEL_PORT"):
                environment[key] = previous.get(key) or str(free_port())
        write_json(
            local / "services-env.json",
            {
                k: v
                for k, v in environment.items()
                if k.startswith("EAB_REDIS_")
                or k.startswith("EAB_PUBLIC_")
                or k == "EAB_RESEARCH_REDIS_PASSWORD"
            },
        )
        os.environ.update(environment)
        if TYPE_CHECKING:
            from .._internal.prepare import prepare
        else:
            from prepare import prepare

        resolved = prepare(root)
        if settings["domain"] == "information_retrieval":
            cache = local / "corpus-cache"
            if not cache.exists():
                staging = local / "corpus-cache.staging"
                shutil.copytree(
                    root / "resources/hf-datasets", staging, dirs_exist_ok=True
                )
                staging.rename(cache)
            environment.update(
                PYTHONPATH=str(project_root()),
                PYTHONDONTWRITEBYTECODE="1",
                HF_DATASETS_CACHE=str(cache),
                HF_HUB_OFFLINE="1",
                HF_DATASETS_OFFLINE="1",
                EVALUATION_API_BASE=os.environ["EAB_BASE_URL"],
                EVALUATION_API_KEY=os.environ["EAB_API_KEY"],
                EVALUATION_MODEL=os.environ["EAB_MODEL"].removeprefix("openai/"),
            )
            commands = [
                (
                    "search",
                    [
                        str(root / "dependencies/upstream/.venv/bin/python"),
                        "-B",
                        "-m",
                        "evolution_tasks.evoagentbench_research._internal.research_services",
                        "--domain-config",
                        resolved["domain_config"],
                        "--lexical-index",
                        str(root / service_config["lexical_index"]),
                        "--upstream-tools",
                        str(
                            root
                            / "dependencies/upstream/benchmark/src/utils"
                            / "browsecomp-plus-tools"
                        ),
                        "--port",
                        environment["EAB_PUBLIC_MCP_PORT"],
                    ],
                ),
                (
                    "model",
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "evolution_tasks.evoagentbench_research._internal.research_model_proxy",
                        "--scopes-dir",
                        str(root / ".local/model-scopes"),
                        "--port",
                        environment["EAB_PUBLIC_MODEL_PORT"],
                    ],
                ),
            ]
            for name, command in commands:
                log = (local / f"{name}.log").open("ab")
                os.chmod(log.name, 0o600)
                streams.append(log)
                children.append(
                    subprocess.Popen(
                        command,
                        cwd=root,
                        env=environment,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                )
        write_json(
            local / "services.json",
            {
                "owner": suffix,
                "pid": os.getpid(),
                "containers": containers,
                "child_pids": [child.pid for child in children],
                "status": "starting",
                "root": str(root),
            },
        )
        # No model requests: readiness probes only the service ports and Redis.
        ports = [int(environment["EAB_REDIS_PORT"])]
        if settings["domain"] == "information_retrieval":
            ports.extend(
                int(environment[k])
                for k in (
                    "EAB_PUBLIC_REDIS_PORT",
                    "EAB_PUBLIC_MCP_PORT",
                    "EAB_PUBLIC_MODEL_PORT",
                )
            )
        deadline = time.monotonic() + 300
        while True:
            if stopping:
                return
            if any(child.poll() is not None for child in children):
                raise RuntimeError(
                    "Service failed during readiness; inspect private logs"
                )
            try:
                for port in ports:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        pass
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Task services did not become ready") from None
                time.sleep(1)
        write_json(
            local / "services.json",
            {
                "owner": suffix,
                "pid": os.getpid(),
                "status": "ready",
                "containers": containers,
                "child_pids": [child.pid for child in children],
            },
        )
        print(
            "Task service ports are ready; real execution still requires smoke.",
            flush=True,
        )
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(
                    "Task service exited; inspect private .local service logs"
                )
            time.sleep(1)
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
        for name in containers:
            subprocess.run(
                ["docker", "rm", "-f", name],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=30,
                env=docker_env,
            )
        for stream in streams:
            stream.close()
        write_json(local / "services.json", {"owner": suffix, "status": "stopped"})
        lock.close()


if __name__ == "__main__":
    main()
