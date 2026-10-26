"""校验基线用量及固定配置边界的协议测试。"""

import importlib
import importlib.metadata
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from benchmarks.evoagentbench.runners.official_baseline import load_baseline_config
from benchmarks.evoagentbench.runners.official_baseline_worker import read_usage


@pytest.mark.asyncio
async def test_research_baseline_connects_to_official_sse_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指定版本 Nanobot 的 HTTP 客户端必须适配官方检索 SSE 端点。"""
    from benchmarks.evoagentbench.runners import official_tool_transport
    from mcp.client import sse, streamable_http

    url = "http://127.0.0.1:19101/mcp"
    seen: list[str] = []

    @asynccontextmanager
    async def fake_sse(address: str, **kwargs: Any) -> AsyncIterator[tuple[str, str]]:
        seen.append(address)
        yield "read", "write"

    def reject_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Nanobot attempted streamable HTTP against SSE")

    class Registry:
        async def execute(self, name: str, params: dict[str, Any]) -> str:
            return "ok"

    actual_import = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(ToolRegistry=Registry)
            if name == "nanobot.agent.tools.registry"
            else actual_import(name)
        ),
    )
    monkeypatch.setattr(sse, "sse_client", fake_sse)
    monkeypatch.setattr(streamable_http, "streamable_http_client", reject_http)
    official_tool_transport.install_transport({"kind": "research", "url": url})
    patched_client: Any = streamable_http.streamable_http_client
    async with patched_client(url, http_client=None) as streams:
        assert streams == ("read", "write", None)
    assert seen == [url]


def test_baseline_tools_inherit_selected_docker_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.evoagentbench.runners.official_baseline_worker import (
        subprocess_environment,
    )

    monkeypatch.setenv("DOCKER_HOST", "unix:///fixture/docker.sock")
    monkeypatch.setenv("UNRELATED_API_KEY", "must-not-pass")
    env = subprocess_environment({}, tmp_path)
    assert env["DOCKER_HOST"] == "unix:///fixture/docker.sock"
    assert "UNRELATED_API_KEY" not in env


def test_baseline_environment_and_archives_exclude_unrelated_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """官方收集过程保持私有，仅脱敏消息进入产物。"""
    from benchmarks.evoagentbench.runners import official_baseline_worker as worker

    key = 'provider-"quoted"-credential'
    unrelated = "trace-only-credential"
    monkeypatch.setenv("EAB_API_KEY", key)
    monkeypatch.setenv("OTEL_AUTHORIZATION", unrelated)
    prior_home = os.environ.get("HOME")
    config = {"providers": {"custom": {"apiKey": "${EAB_API_KEY}"}}}
    upstream = SimpleNamespace(_config={"domain": {"name": "test"}})
    output = tmp_path / "archive"

    class Adapter:
        _get_subprocess_env: Any

        def setup_mcp(self, *args: Any) -> None:
            pass

        def call_agent(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            env = self._get_subprocess_env()
            assert env["EAB_API_KEY"] == key
            assert "OTEL_AUTHORIZATION" not in env
            assert env["HOME"] != prior_home
            return {"response": key + unrelated}

        def collect_session(self, session: str, directory: Path) -> None:
            assert directory != output
            assert not (output / "session.jsonl").exists()
            (directory / "session.jsonl").write_text(
                json.dumps({"role": "tool", "content": key + unrelated})
            )

        def teardown_mcp(self) -> None:
            pass

    modules = {
        "config": upstream,
        "agents.nanobot.nanobot": SimpleNamespace(NanobotAdapter=Adapter),
    }
    monkeypatch.setattr(importlib, "import_module", lambda name: modules[name])
    monkeypatch.setattr(
        importlib.metadata, "version", lambda name: worker.NANOBOT_VERSION
    )
    result = worker.call_nanobot(
        "prompt", config, 10, output, environment={"kind": "research"}
    )
    assert result["agent_result"]["response"] == "[REDACTED][REDACTED]"
    assert result["session_jsonl"] == (output / "session.jsonl").read_text()
    assert os.environ.get("HOME") == prior_home
    for path in output.iterdir():
        assert key not in path.read_text()
        assert unrelated not in path.read_text()
        assert "[REDACTED]" in path.read_text()


def test_redaction_preserves_json_with_escaped_credentials() -> None:
    """解析后的值脱敏应处理引号、换行和嵌套消息。"""
    from benchmarks.evoagentbench.runners.official_baseline_worker import redact

    secret = 'private"key\nvalue'
    result = redact({"messages": [{"role": "system", "content": secret}]}, [secret])
    assert json.loads(json.dumps(result))["messages"][0]["content"] == "[REDACTED]"


def test_domain_baseline_preserves_trials_and_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """现有领域执行器应绑定基线身份，并复用有效试次。"""
    from benchmarks.evoagentbench.common.identity import file_digest
    from benchmarks.evoagentbench.data.models import Case, Dataset
    from benchmarks.evoagentbench.domains import domain_adapter
    from benchmarks.evoagentbench.domains.domain_settings import DomainSettings
    from benchmarks.evoagentbench.runners import official_baseline
    from evolution.models import CaseAttempt, DatasetRole, ExecutionMetadata

    config = tmp_path / "baseline.yaml"
    config.write_text("fixed baseline config")
    target = tmp_path / "target"
    target.mkdir()
    settings = DomainSettings(
        domain="information_retrieval",
        upstream_repo=tmp_path,
        upstream_python=Path(sys.executable),
        domain_config=tmp_path / "domain.yaml",
        runtime_config=tmp_path / "runtime.yaml",
        artifact_dir=tmp_path / "evidence",
        runner="official_baseline",
        official_agent_config=config,
        test_trials=2,
    )
    dataset = tmp_path / "test.json"
    dataset.write_text(
        Dataset(
            role="test",
            upstream_revision=settings.upstream_revision,
            source_sha256="source",
            cases=(Case(id="selected", prompt="public prompt"),),
        ).model_dump_json()
    )
    monkeypatch.setattr(official_baseline, "load_baseline_config", lambda _: {})
    monkeypatch.setattr(domain_adapter, "experiment_identity", lambda *_: "protocol")
    requests = []

    def upstream(
        settings: DomainSettings, operation: str, ids: list[str], **kwargs: Any
    ) -> dict[str, Any]:
        assert settings.runner == "official_baseline"
        assert settings.official_agent_config == config
        requests.append((operation, ids))
        return CaseAttempt(
            result="answer",
            tracelog={"status": "succeeded", "score": 1},
            metadata=ExecutionMetadata(total_tokens=10, duration_seconds=1.5),
        ).model_dump(mode="json")

    monkeypatch.setattr(domain_adapter, "upstream", upstream)
    executor = domain_adapter.DomainExecutor(settings)
    result = executor.execute(target, dataset, DatasetRole.TEST, max_retries=0)
    case = result.cases[0]
    assert case.id == "selected"
    assert len(case.trials) == 2 and not case.attempts
    assert case.metadata.total_tokens == 20
    assert case.metadata.duration_seconds == 3
    assert requests == [("trial", ["selected"])] * 2
    assert isinstance(case.tracelog, dict)
    assert case.tracelog["protocol"] == "protocol"
    assert case.tracelog["target_sha256"] == file_digest(config)
    assert executor.execute(target, dataset, DatasetRole.TEST, max_retries=0) == result
    assert len(requests) == 2


def test_usage_missing_is_unknown(tmp_path: Path) -> None:
    """官方 adapter 默认的零计数不能证明实际 token 消耗。"""
    assert read_usage(tmp_path / "absent")[0] is None


@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": 0}])
def test_partial_usage_is_unknown(tmp_path: Path, usage: object) -> None:
    """缺少完整供应商 token 计数的响应属于不完整证据。"""
    path = tmp_path / "usage.jsonl"
    path.write_text(
        json.dumps({"event": "start", "id": "a"})
        + "\n"
        + json.dumps({"event": "end", "id": "a", "usage": usage})
    )
    assert read_usage(path)[0] is None


def test_usage_counts_all_calls_and_rejects_interruption(tmp_path: Path) -> None:
    """每次供应商调用都计入实际用量，包括记忆相关调用。"""
    path = tmp_path / "usage.jsonl"
    records = []
    for identifier in ("a", "b"):
        records.extend(
            [
                {"event": "start", "id": identifier},
                {
                    "event": "end",
                    "id": identifier,
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                },
            ]
        )
    path.write_text("\n".join(json.dumps(row) for row in records))
    assert read_usage(path)[0] == {
        "prompt_tokens": 6,
        "completion_tokens": 4,
        "total_tokens": 10,
    }
    with path.open("a") as stream:
        stream.write('\n{"event":"start","id":"c"}')
    assert read_usage(path)[0] is None


def test_config_rejects_literal_credentials_and_custom_command(tmp_path: Path) -> None:
    """未固定版本的启动器和持久化凭证不得进入基线。"""
    path = tmp_path / "config.yaml"
    value = {
        "nanobot_version": "0.1.4.post3",
        "max_retries": 0,
        "agent": {
            "model": "model",
            "provider": "custom",
            "temperature": 0.6,
            "maxTokens": 100,
            "providers": {"custom": {"apiKey": "literal"}},
        },
    }
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="environment references"):
        load_baseline_config(path)


def test_pinned_cli_preserves_task_config_and_message(tmp_path: Path) -> None:
    """转换新的 CLI 参数时，不得丢失配置或误解析提示词内容。"""
    from benchmarks.evoagentbench.runners.official_baseline_worker import (
        prepare_cli_arguments,
    )

    home = tmp_path / "private-home"
    home.mkdir()
    workspace = home / "task"
    workspace.mkdir()
    config = {
        "agents": {"defaults": {"workspace": str(workspace), "model": "test-model"}},
        "providers": {"custom": {"apiKey": "test-key"}},
        "tools": {"mcpServers": {"local": {"command": "test"}}},
    }
    source = workspace / "config.json"
    source.write_text(json.dumps(config))
    message = "do not interpret --workspace or --config in this prompt"
    args = ["agent", "--message", message, "--session", "trial", "--no-markdown"]
    result = prepare_cli_arguments(
        [*args, "--workspace", str(workspace), "--config", str(source)], home
    )
    assert result == args
    destination = home / ".nanobot/config.json"
    assert json.loads(destination.read_text()) == config
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".nanobot").exists()


def test_pinned_cli_rejects_mismatched_workspace(tmp_path: Path) -> None:
    from benchmarks.evoagentbench.runners.official_baseline_worker import (
        prepare_cli_arguments,
    )

    source = tmp_path / "config.json"
    source.write_text(json.dumps({"agents": {"defaults": {"workspace": "other"}}}))
    with pytest.raises(ValueError, match="workspace"):
        prepare_cli_arguments(
            [
                "agent",
                "--message",
                "test",
                "--session",
                "trial",
                "--workspace",
                str(tmp_path),
                "--config",
                str(source),
            ],
            tmp_path,
        )
    assert not (tmp_path / ".nanobot/config.json").exists()


@pytest.mark.asyncio
async def test_custom_provider_requests_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """custom SDK 路径必须提供实际用量，不传入仅适用于 LiteLLM 的参数。"""
    from benchmarks.evoagentbench.runners import official_baseline_worker as worker

    ledger = tmp_path / "usage.jsonl"
    for name in ("EAB_TOOL_ENV_FILE", "EAB_REDACTION_FILE"):
        path = tmp_path / name
        path.write_text("{}" if "TOOL" in name else '["private-key"]')
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("EAB_USAGE_FILE", str(ledger))
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    class Completions:
        async def create(self, **kwargs: Any) -> Any:
            assert "num_retries" not in kwargs
            return SimpleNamespace(
                usage=SimpleNamespace(model_dump=lambda: usage),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            model_dump=lambda: {
                                "role": "assistant",
                                "content": "answer private-key",
                            }
                        )
                    )
                ],
            )

    class CustomProvider:
        def __init__(self) -> None:
            self._client = SimpleNamespace(
                chat=SimpleNamespace(completions=Completions())
            )

    class Registry:
        async def execute(self, *args: Any) -> str:
            return "ok"

    modules = {
        "benchmarks.evoagentbench.runners.official_tool_transport": SimpleNamespace(
            install_transport=lambda config: None
        ),
        "nanobot.providers.litellm_provider": SimpleNamespace(acompletion=None),
        "nanobot.agent.tools.registry": SimpleNamespace(ToolRegistry=Registry),
        "nanobot.providers.custom_provider": SimpleNamespace(
            CustomProvider=CustomProvider
        ),
        "nanobot.cli.commands": SimpleNamespace(app=lambda: None),
    }
    monkeypatch.setattr(importlib, "import_module", lambda name: modules[name])
    monkeypatch.setattr(worker, "prepare_cli_arguments", lambda args, home: args)
    monkeypatch.setattr(sys, "argv", ["worker", "--nanobot", "agent"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    worker._nanobot_cli()
    await CustomProvider()._client.chat.completions.create(model="test", messages=[])
    actual, records = worker.read_usage(ledger)
    assert actual == usage
    assert [row["event"] for row in records] == ["start", "end"]
    assert records[1]["output"] == [
        {"role": "assistant", "content": "answer [REDACTED]"}
    ]
    await Registry().execute("exec", {"command": "echo private-key"})
    tool = worker.read_usage(ledger)[1][-1]
    assert tool["input"] == {"command": "echo [REDACTED]"}
    assert "private-key" not in ledger.read_text()
    assert tool["output"] == "ok"
