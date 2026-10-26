"""由上游 Python 解释器执行的私有官方生命周期 worker。

不导入 Runtime，隐藏任务记录和评分器状态不得进入模型工具。
"""

import argparse
import hashlib
import importlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

ADAPTERS = {
    "knowledge_work": ("gdpval", "GDPValAdapter"),
    "information_retrieval": ("browsecomp_plus", "BrowseCompPlusAdapter"),
}


def load_adapter(settings: dict[str, Any]) -> Any:
    """加载指定版本的官方代码，并解析其私有配置。"""
    if settings["domain"] not in ADAPTERS:
        raise ValueError("Unsupported benchmark domain")
    root = Path(settings["upstream_repo"])
    revision_file = root / "REVISION"
    revision = (
        revision_file.read_text().strip()
        if revision_file.is_file()
        else subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
    )
    if revision != settings["upstream_revision"]:
        raise ValueError("Upstream revision mismatch")
    sys.path.insert(0, str(root / "benchmark/src"))
    if settings["domain"] == "knowledge_work":
        if not settings.get("hf_cache_dir"):
            raise ValueError("GDPVal requires an explicit frozen HF cache directory")
        cache = Path(os.environ.get("EAB_TASK_HF_CACHE", settings["hf_cache_dir"]))
        os.environ.update(
            {
                "HF_HOME": str(cache),
                "HF_HUB_CACHE": str(cache / "hub"),
                "HF_DATASETS_CACHE": str(cache / "datasets"),
                "HF_HUB_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
            }
        )
    import yaml

    config: Any = importlib.import_module("config")
    config._config = {
        "docker": "docker",
        "docker_sock": settings.get("docker_socket", "/var/run/docker.sock"),
        "domain": {"name": settings["domain"]},
        "agent": {},
    }
    path = Path(settings["domain_config"])
    raw = yaml.safe_load(path.read_text())
    config._domain_configs[settings["domain"]] = config._resolve_dict(
        config._resolve_env_vars(raw), path.parent
    )
    if settings["domain"] == "information_retrieval" and raw.get("mcp_server", {}).get(
        "auto_start", False
    ):
        raise ValueError("Use a separately managed SSE server with auto_start=false")
    module, name = ADAPTERS[settings["domain"]]
    return getattr(
        importlib.import_module(f"domains.{settings['domain']}.{module}"), name
    )()


def public_environment(domain: str, env: dict[str, Any]) -> dict[str, str]:
    """仅导出允许的字段，不序列化任务记录、评分细则或答案。"""
    if domain == "information_retrieval":
        server = env["mcp_servers"]["bcp-search"]
        if server["type"] != "sse":
            raise ValueError("Official research tool must use SSE")
        return {"kind": "research", "url": server["url"]}
    if domain == "knowledge_work":
        return {
            "kind": "knowledge",
            "container": env["execution_container"],
            "workspace": env["workspace_dir"],
        }
    raise ValueError("Unsupported benchmark domain")


def normalized_verdict(value: dict[str, Any], threshold: float = 0) -> dict[str, Any]:
    """将基础设施故障作为执行错误处理，不记为测得的零分。"""
    error = value.get("error")
    if error and error != "no_artifacts_found":
        raise RuntimeError("Official evaluator failed; inspect private artifacts")
    score = value.get("reward")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("Official reward must be numeric")
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("Official reward is outside [0,1]")
    normalized = {
        "score": float(score),
        "method": value.get("method", "official"),
        "passed": score > threshold,
        "pass_threshold": threshold,
    }
    if error:
        normalized["error"] = error
    return normalized


def check_retrieval_judge(directory: Path) -> None:
    """已配置的评分模型失败时视为评分错误，不按精确匹配失败记零分。"""
    path = directory / "verifier/details.json"
    if path.is_file():
        judge = json.loads(path.read_text()).get("judge_result", {})
        if judge and judge.get("parse_error"):
            raise RuntimeError(
                "Retrieval judge failed; inspect private grading evidence"
            )


def archive_retrieval_details(request: dict[str, Any], directory: Path) -> None:
    """将原始私有评分证据保留在模型可读资产之外。"""
    root = request["settings"].get("artifact_dir")
    source = directory / "verifier/details.json"
    if root and source.is_file():
        dest = Path(root) / "verifier-private" / request["job_tag"]
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, dest / "details.json")
        (dest / "details.json").chmod(0o600)


def verify_response(
    adapter: Any,
    task: dict[str, Any],
    env: dict[str, Any],
    directory: Path,
    request: dict[str, Any],
    response: str,
) -> dict[str, Any]:
    """校验检索答案提交，并保留已配置评分模型的失败信息。"""
    retrieval = request["settings"]["domain"] == "information_retrieval"
    if retrieval:
        # 此 worker 在上游虚拟环境中运行，不导入 Runtime。
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        reason = importlib.import_module(
            "benchmarks.evoagentbench.domains.retrieval_feedback"
        ).no_answer_reason(response)
        if reason:
            return {"reward": 0.0, "method": "no_answer_guard", "reason": reason}
    try:
        verdict = adapter.verify(
            task, env, directory, agent_result={"response": response}
        )
    finally:
        if retrieval:
            archive_retrieval_details(request, directory)
    if retrieval:
        check_retrieval_judge(directory)
    return cast(dict[str, Any], verdict)


def office_container(settings: dict[str, Any], env: dict[str, Any], name: str) -> str:
    """仅挂载官方参考文件和输出工作区，不挂载私有记录。"""
    workspace = str(Path(env["workspace_dir"]).resolve())
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=2g",
            "--cpus=2",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/tmp",
            "--mount",
            f"type=bind,src={workspace},dst={workspace}",
            "--workdir",
            workspace,
            "--entrypoint=/bin/sh",
            settings["execution_image"],
            "-c",
            "while :; do sleep 3600; done",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return name


def run_runtime(
    request: dict[str, Any], prompt: str, env: dict[str, Any], directory: Path
) -> dict[str, Any]:
    """在独立依赖环境中以仅含公开数据的请求启动 Runtime。"""
    settings = request["settings"]
    payload = {
        "target": request["target"],
        "runtime_config": settings["runtime_config"],
        "prompt": prompt,
        "timeout": settings["case_timeout_seconds"],
        "domain_environment": public_environment(settings["domain"], env),
        "image_archive": str(
            Path(settings["artifact_dir"]) / "media" / request["job_tag"]
        )
        if settings.get("artifact_dir")
        else None,
    }
    source, result = (
        directory / "runtime-request.json",
        directory / "runtime-result.json",
    )
    source.write_text(json.dumps(payload))
    if settings.get("research_execution_image"):
        from evolution_tasks.evoagentbench_research._internal.research_sandbox import (
            run as isolated_research,
        )

        return isolated_research(request, payload, directory)
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith(("EVALUATION_", "OPENROUTER_")):
            environment.pop(key)
    subprocess.run(
        [
            request["runtime_python"],
            "-m",
            "benchmarks.evoagentbench.runners.runtime_worker",
            "--request",
            str(source),
            "--result",
            str(result),
        ],
        check=True,
        timeout=settings["case_timeout_seconds"] + 60,
        env=environment,
    )
    return cast(dict[str, Any], json.loads(result.read_text()))


def run_baseline(
    request: dict[str, Any], prompt: str, env: dict[str, Any], directory: Path
) -> dict[str, Any]:
    """复用基线 worker 提供的原始官方 Nanobot 执行器。"""
    import yaml

    baseline = importlib.import_module(
        "benchmarks.evoagentbench.runners.official_baseline_worker"
    )

    settings = request["settings"]
    config = yaml.safe_load(Path(settings["official_agent_config"]).read_text())[
        "agent"
    ]
    started = time.monotonic()
    result = baseline.call_nanobot(
        prompt,
        config,
        settings["case_timeout_seconds"],
        directory / "baseline",
        cwd=Path(env["workspace_dir"]) if env.get("workspace_dir") else None,
        mcp_servers=env.get("mcp_servers"),
        disabled_tools=env.get("disabled_tools"),
        environment=public_environment(settings["domain"], env),
    )
    if result.get("usage") is None:
        raise ValueError("Official baseline usage is missing or partial")
    status = result["agent_result"].get("completion_status")
    return {
        "result": result["agent_result"].get("response", ""),
        "tracelog": {
            "status": "succeeded" if status == "completed" else "failed",
            "official": result,
            "agent_config_sha256": hashlib.sha256(
                Path(settings["official_agent_config"]).read_bytes()
            ).hexdigest(),
        },
        "metadata": {
            "total_tokens": result["usage"]["total_tokens"],
            "duration_seconds": time.monotonic() - started,
        },
    }


def archive_outputs(
    request: dict[str, Any],
    env: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """归档候选产物，排除私有评分报告和参考文件。"""
    root = request["settings"].get("artifact_dir")
    if not root:
        return
    from benchmarks.evoagentbench.domains.office_feedback import check_archived_file

    destination = Path(root) / "outputs" / request["job_tag"]
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    files: list[Path] = []
    if env.get("workspace_dir"):
        workspace = Path(env["workspace_dir"])
        references = {Path(path).resolve() for path in env.get("reference_paths", [])}
        files.extend(
            path
            for path in workspace.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.resolve() not in references
            and not any(
                part.startswith(".") for part in path.relative_to(workspace).parts
            )
        )
    index = []
    for number, path in enumerate(files):
        target = destination / f"{number:04d}-{path.name}"
        shutil.copyfile(path, target)
        target.chmod(0o600)
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        index.append(
            {
                "name": path.name,
                "path": str(target),
                "sha256": digest.hexdigest(),
                "bytes": target.stat().st_size,
                "check": check_archived_file(target),
            }
        )
    result["tracelog"]["artifacts"] = index
    record = destination / "execution.json"
    temporary = record.with_suffix(".pending")
    temporary.write_text(json.dumps(result))
    temporary.chmod(0o600)
    temporary.replace(record)
    record.chmod(0o600)


def archive_verdict(request: dict[str, Any], verdict: dict[str, Any]) -> None:
    """私有保存原始评分证据，不写入候选可见的执行轨迹。"""
    root = request["settings"].get("artifact_dir")
    if not root:
        return
    destination = Path(root) / "outputs" / request["job_tag"]
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    record = destination / "official-verdict.private.json"
    with record.open("x", encoding="utf-8") as stream:
        record.chmod(0o600)
        json.dump(verdict, stream, ensure_ascii=False)


def preflight_task(task: dict[str, Any], settings: dict[str, Any]) -> None:
    """要求办公任务的输入文件在评测前下载完成。"""
    if settings["domain"] == "knowledge_work":
        configuration = importlib.import_module("config").get_domain_config(
            "knowledge_work"
        )
        refs = Path(configuration["reference_dir"]) / task["task_id"]
        if any(
            not (refs / Path(path).name).is_file()
            for path in task.get("reference_files", [])
        ):
            raise ValueError("GDP references must be downloaded before freezing")


def trial(
    adapter: Any, task: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    """保持环境可用直到验证结束，并在 finally 中回收。"""
    settings = request["settings"]
    if settings["domain"] not in ADAPTERS:
        raise ValueError("Unsupported benchmark domain")
    workers = (
        Path(settings["artifact_dir"]) / "workers"
        if settings.get("research_execution_image")
        else None
    )
    if workers is not None:
        workers.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="eab-domain-", dir=workers) as temporary:
        directory = Path(temporary)
        job = directory / request["job_tag"]
        job.mkdir()
        (job / f"{task['name']}__trial_1").mkdir()
        task["_job_dir"] = str(job)
        env: dict[str, Any] = {}
        with ExitStack() as cleanup:
            cleanup.callback(adapter.finalize)
            adapter.initialize(SimpleNamespace())
            cleanup.callback(adapter.post_task_trials, task)
            preflight_task(task, settings)
            adapter.pre_task_trials(task)
            env = adapter.setup(task, "agentsystem-" + uuid4().hex, 1)

            cleanup.callback(adapter.cleanup, task, env)
            if settings["domain"] == "knowledge_work":
                expected = {Path(path).name for path in task.get("reference_files", [])}
                available = {Path(path).name for path in env.get("reference_paths", [])}
                if not expected <= available:
                    raise ValueError(
                        "Official setup did not provide every reference file"
                    )
            if settings["domain"] == "knowledge_work":
                env["execution_container"] = office_container(
                    settings, env, request["container"]
                )
                cleanup.callback(
                    subprocess.run,
                    ["docker", "rm", "-f", env["execution_container"]],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            prompt = adapter.build_prompt(task, env)
            runner = (
                run_baseline
                if settings["runner"] == "official_baseline"
                else run_runtime
            )
            result = runner(request, prompt, env, directory)
            # Candidate output cannot claim a host-owned grading receipt.
            # Only verify_response below may populate this trusted field.
            result["tracelog"].pop("verifier", None)
            if result["tracelog"]["status"] != "succeeded":
                archive_outputs(request, env, result)
                raise RuntimeError("Agent runtime did not succeed")
            archive_outputs(request, env, result)
            try:
                verdict = verify_response(
                    adapter, task, env, directory, request, result["result"]
                )
                archive_verdict(request, verdict)
                normalized = normalized_verdict(verdict, adapter.pass_threshold())
                result["tracelog"]["verifier"] = normalized
            finally:
                archive_outputs(request, env, result)
            return result


def run(request: dict[str, Any]) -> dict[str, Any]:
    """派发公开案例导出或一次完整的官方试验生命周期。

    Args:
        request: 监督器请求，包含配置、操作类型和完整任务 ID 列表。

    Returns:
        prepare 返回公开案例定义，trial 返回已验证的执行记录。

    Raises:
        ValueError: 任务集合、领域配置或操作类型无效。
    """
    adapter = load_adapter(request["settings"])
    ids = request["ids"]
    tasks = adapter.load_tasks(SimpleNamespace(split=None, task=",".join(ids)))
    key = "task_id" if request["settings"]["domain"] == "knowledge_work" else "name"
    mapped = {str(task[key]): task for task in tasks}
    if set(mapped) != set(ids) or len(mapped) != len(tasks):
        raise ValueError("Official tasks must exactly cover unique requested IDs")
    if request["operation"] == "prepare":
        # 导出时使用公开占位符，试验初始化时提供实际路径。
        env = {
            "workspace_dir": "/workspace",
            "reference_section": "Official reference files are prepared per trial.",
        }
        return {
            "cases": [
                {"id": key, "prompt": adapter.build_prompt(mapped[key], env)}
                for key in ids
            ]
        }
    if request["operation"] != "trial" or len(ids) != 1:
        raise ValueError("Invalid domain worker operation")
    return trial(adapter, mapped[ids[0]], request)


def main() -> None:
    """读取监督器的私有请求文件，并写入 JSON 结果。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    args.result.write_text(json.dumps(run(json.loads(args.request.read_text()))))


if __name__ == "__main__":
    main()
