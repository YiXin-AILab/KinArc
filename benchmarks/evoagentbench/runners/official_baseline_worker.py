"""隔离的官方 Nanobot adapter 与透明的模型供应商用量记录器。

要求上游 Python 环境安装 benchmark 的 requirements-core/code
及 nanobot-ai==0.1.4.post3；本模块不实现 Agent 循环。
"""

import argparse
import importlib
import importlib.metadata
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

NANOBOT_VERSION = "0.1.4.post3"


def resolve_environment(value: Any) -> Any:
    """仅解析显式环境变量占位符，变量缺失时立即失败。"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ[value[2:-1]]
    if isinstance(value, dict):
        return {key: resolve_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_environment(item) for item in value]
    return value


def prepare_cli_arguments(arguments: list[str], home: Path) -> list[str]:
    """将官方任务参数映射到指定版本 CLI 的私有 HOME 配置。

    指定版本的 Nanobot 仅读取 ~/.nanobot/config.json，需保留
    上游生成的工作区、会话收集机制及全部配置。
    此逻辑仅在单次试验的子进程中运行，不操作用户的 HOME。
    """
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("command", choices=["agent"])
    parser.add_argument("--message", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--no-markdown", action="store_true")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(arguments)
    config = json.loads(args.config.read_text())
    workspace = Path(config["agents"]["defaults"]["workspace"]).resolve()
    if workspace != args.workspace.resolve() or not workspace.is_relative_to(
        home.resolve()
    ):
        raise ValueError("Official task workspace must match its private HOME config")
    destination = home / ".nanobot" / "config.json"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    with open(
        destination, "w", opener=lambda path, flags: os.open(path, flags, 0o600)
    ) as stream:
        json.dump(config, stream)
    result = ["agent", "--message", args.message, "--session", args.session]
    if args.no_markdown:
        result.append("--no-markdown")
    return result


def output_budget_arguments(
    arguments: dict[str, Any], *, uncapped: bool
) -> dict[str, Any]:
    """仅在单次试验显式指定 null 输出预算时移除传输默认值。"""
    result = dict(arguments)
    if uncapped:
        result.pop("max_tokens", None)
        result.pop("max_completion_tokens", None)
    return result


def _nanobot_cli() -> None:
    """记录实际发出的每次生成请求，保留上游 CLI 行为。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    transport = importlib.import_module(
        "benchmarks.evoagentbench.runners.official_tool_transport"
    )
    transport.install_transport(
        json.loads(Path(os.environ["EAB_TOOL_ENV_FILE"]).read_text())
    )
    provider: Any = importlib.import_module("nanobot.providers.litellm_provider")
    original = provider.acompletion
    ledger = Path(os.environ["EAB_USAGE_FILE"])
    secrets = json.loads(Path(os.environ["EAB_REDACTION_FILE"]).read_text())
    registry: Any = importlib.import_module("nanobot.agent.tools.registry")
    execute_tool = registry.ToolRegistry.execute

    async def recorded_tool(self: Any, name: str, params: dict[str, Any]) -> Any:
        started = time.time_ns()
        error_type = None
        output: Any = None
        try:
            output = await execute_tool(self, name, params)
            return output
        except BaseException as error:
            error_type = type(error).__name__
            output = {"error_type": error_type}
            raise
        finally:
            with ledger.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event": "tool",
                            "name": name,
                            "start_ns": started,
                            "end_ns": time.time_ns(),
                            "error_type": error_type,
                            "input": redact(params, secrets),
                            "output": redact(output, secrets),
                        }
                    )
                    + "\n"
                )

    registry.ToolRegistry.execute = recorded_tool

    async def recorded(
        _request: Any = original, _litellm: bool = True, **kwargs: Any
    ) -> Any:
        kwargs = output_budget_arguments(
            kwargs, uncapped=os.environ.get("EAB_UNCAPPED_OUTPUT") == "1"
        )
        identifier = uuid4().hex
        with ledger.open("a") as stream:
            stream.write(
                json.dumps(
                    redact(
                        {
                            "event": "start",
                            "time_ns": time.time_ns(),
                            "id": identifier,
                            "model": kwargs.get("model"),
                            "output_budget": {
                                "max_tokens_present": "max_tokens" in kwargs,
                                "max_completion_tokens_present": "max_completion_tokens"
                                in kwargs,
                            },
                            "messages": kwargs.get("messages"),
                            "tools": kwargs.get("tools"),
                        },
                        secrets,
                    )
                )
                + "\n"
            )
        if _litellm:
            kwargs["num_retries"] = 0
        response = await _request(**kwargs)
        usage = getattr(response, "usage", None)
        with ledger.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "event": "end",
                        "time_ns": time.time_ns(),
                        "id": identifier,
                        "usage": usage.model_dump() if usage is not None else None,
                        "output": redact(
                            [
                                choice.message.model_dump()
                                for choice in response.choices
                            ],
                            secrets,
                        ),
                        "finish_reasons": [
                            getattr(choice, "finish_reason", None)
                            for choice in response.choices
                        ],
                    }
                )
                + "\n"
            )
        return response

    provider.acompletion = recorded
    # Nanobot 的 custom provider 绕过 LiteLLM，直接调用 OpenAI SDK。
    custom: Any = importlib.import_module("nanobot.providers.custom_provider")
    original_init = custom.CustomProvider.__init__

    def recorded_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        create = self._client.chat.completions.create

        async def recorded_create(**values: Any) -> Any:
            return await recorded(_request=create, _litellm=False, **values)

        self._client.chat.completions.create = recorded_create

    custom.CustomProvider.__init__ = recorded_init
    sys.argv = ["nanobot", *prepare_cli_arguments(sys.argv[2:], Path.home())]
    importlib.import_module("nanobot.cli.commands").app()


def read_usage(path: Path) -> tuple[dict[str, int] | None, list[dict[str, Any]]]:
    """未知、部分缺失或格式错误的用量保持未知状态，不记为零。"""
    records = (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )
    starts = [row["id"] for row in records if row["event"] == "start"]
    ends = [row for row in records if row["event"] == "end"]
    if not starts or sorted(starts) != sorted(row["id"] for row in ends):
        return None, records
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    total = dict.fromkeys(keys, 0)
    for row in ends:
        usage = row.get("usage")
        if not isinstance(usage, dict) or any(
            type(usage.get(k)) is not int or usage[k] < 0 for k in keys
        ):
            return None, records
        for key in keys:
            total[key] += usage[key]
    return total, records


def redact(value: Any, secrets: list[str]) -> Any:
    """序列化前对解析后的字符串脱敏，包括转义后的凭证。"""
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
    elif isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    elif isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


def subprocess_environment(config: Any, home: Path) -> dict[str, str]:
    """仅传递平台必要变量及配置中显式引用的供应商环境变量。"""
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }

    def references(value: Any) -> None:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            allowed.add(value[2:-1])
        elif isinstance(value, dict):
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)

    references(config)
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "EAB_USAGE_FILE": str(home / "usage.jsonl"),
            "EAB_REDACTION_FILE": str(home / "redactions.json"),
            "EAB_TOOL_ENV_FILE": str(home / "tool-environment.json"),
        }
    )
    if (
        isinstance(config, dict)
        and "maxTokens" in config
        and config["maxTokens"] is None
    ):
        env["EAB_UNCAPPED_OUTPUT"] = "1"
    return env


def _secret_values(resolved: dict[str, Any]) -> list[str]:
    values = [
        value
        for key, value in os.environ.items()
        if any(
            part in key.upper()
            for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION")
        )
    ]
    values.extend(
        value
        for provider in resolved.get("providers", {}).values()
        for value in provider.values()
        if isinstance(value, str)
    )
    return values


def _mcp_secrets(value: Any, sensitive: bool = False) -> list[str]:
    if isinstance(value, str):
        return [value] if sensitive else []
    if isinstance(value, dict):
        return [
            secret
            for key, item in value.items()
            for secret in _mcp_secrets(
                item,
                sensitive
                or key.lower() in {"env", "headers"}
                or any(
                    part in key.lower()
                    for part in ("key", "token", "secret", "password", "authorization")
                ),
            )
        ]
    if isinstance(value, list):
        return [secret for item in value for secret in _mcp_secrets(item, sensitive)]
    return []


def call_nanobot(
    prompt: str,
    config: dict[str, Any],
    timeout: float,
    output_dir: Path,
    cwd: Path | None = None,
    mcp_servers: dict[str, Any] | None = None,
    disabled_tools: list[str] | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """在新的私有 HOME 中调用官方 adapter，并在回收前收集结果。

    仅在独立 worker 进程中调用：HOME 和上游配置属于当前进程
    状态；临时凭证不得复制到 output_dir。
    """
    if importlib.metadata.version("nanobot-ai") != NANOBOT_VERSION:
        raise ValueError("Install nanobot-ai==" + NANOBOT_VERSION)
    if environment is None or environment.get("kind") not in {
        "knowledge",
        "research",
    }:
        raise ValueError("Official baseline requires isolated public tool environment")
    env_keys = ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "EAB_USAGE_FILE")
    previous_env = {key: os.environ.get(key) for key in env_keys}
    previous_tempdir = tempfile.tempdir
    output_dir.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="eab-official-") as temporary:
        home = Path(temporary)
        os.environ["HOME"] = str(home)
        os.environ["TMPDIR"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(home / "config")
        tempfile.tempdir = str(home)
        os.environ["EAB_USAGE_FILE"] = str(home / "usage.jsonl")
        launcher = home / "nanobot-recorded"
        launcher.write_text(
            f"#!{sys.executable}\nimport runpy, sys\nsys.argv.insert(1, '--nanobot')\n"
            f"runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')\n"
        )
        launcher.chmod(0o700)
        resolved = resolve_environment(config)
        secrets = _secret_values(resolved) + _mcp_secrets(mcp_servers)
        provider_secrets = [
            value
            for provider in resolved.get("providers", {}).values()
            for value in provider.values()
            if isinstance(value, str)
        ]
        (home / "redactions.json").write_text(json.dumps(provider_secrets + secrets))
        (home / "tool-environment.json").write_text(json.dumps(environment))
        module: Any = importlib.import_module("config")
        # 使用显式配置替换注册配置，不加载上游默认 YAML。
        module._config = {
            **(module._config or {}),
            "agent": {
                **resolved,
                "name": "nanobot",
                "command": str(launcher),
            },
        }
        agent = importlib.import_module("agents.nanobot.nanobot").NanobotAdapter()
        # 仅修改 adapter 的子进程环境，保留其 Agent 循环。
        agent._get_subprocess_env = lambda: subprocess_environment(config, home)
        session = "eab-" + uuid4().hex
        try:
            agent.setup_mcp(mcp_servers or {}, disabled_tools)
            result = agent.call_agent(
                prompt, session, timeout=timeout, cwd=str(cwd or home)
            )
            collected = home / "collected"
            collected.mkdir()
            agent.collect_session(session, collected)
            usage, calls = read_usage(home / "usage.jsonl")
            value = {
                "input": prompt,
                "agent_result": result,
                "usage": usage,
                "llm_calls": calls,
                "nanobot_version": NANOBOT_VERSION,
                "tool_transport": "official-nanobot-with-isolated-tools-v1",
            }
            redacted = redact(value, secrets)
            if not isinstance(redacted, dict):
                raise ValueError("Invalid baseline result")
            value = redacted
            session_file = collected / "session.jsonl"
            if session_file.exists():
                sanitized = [
                    json.dumps(redact(json.loads(line), secrets))
                    for line in session_file.read_text().splitlines()
                ]
                value["session_jsonl"] = "\n".join(sanitized) + "\n"
                (output_dir / "session.jsonl").write_text(value["session_jsonl"])
            # 上游 result.response 是完整标准输出，不能当作结构化 AgentResult。
            (output_dir / "result.json").write_text(
                json.dumps(value, ensure_ascii=False)
            )
            return value
        finally:
            agent.teardown_mcp()
            tempfile.tempdir = previous_tempdir
            for key, previous in previous_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous


def main() -> None:
    """实现受监督 worker 的文件通信协议。"""
    if len(sys.argv) > 1 and sys.argv[1] == "--nanobot":
        _nanobot_cli()
        return
    raise ValueError("Use the supervised domain worker for office/retrieval trials")


if __name__ == "__main__":
    main()
