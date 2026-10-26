"""验证真实图片字节、资产服务、中间件和模型输入媒体映射。"""

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest
from agentsystem.asset.fetcher import SafeMediaFetcher
from agentsystem.asset.local_store import LocalAssetStore
from agentsystem.asset.service import AssetService
from agentsystem.common.errors import ToolError
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.llm.client import LLMClient, LLMClientConfig
from agentsystem.llm.models import (
    LLMEvent,
    LLMGenerationConfig,
    LLMRequest,
    LLMStart,
    MediaTransport,
)
from agentsystem.message.models import AssetSource, Base64Source, ImageBlock
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewarePoint,
    TurnMiddlewarePayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.runtime.state import AgentState
from application.evoagentbench_knowledge_agent.plugins.execution import CommandResult
from application.evoagentbench_knowledge_agent.plugins.office_images import OfficeImages

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6N0AAAAASUVORK5CYII="
)


class LocalFixtureEnvironment:
    """仅在 pytest 工作区中运行测试生成的辅助代码。"""

    async def run(
        self, command: str, cwd: str, timeout_seconds: float
    ) -> CommandResult:
        child = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(timeout_seconds):
            output, error = await child.communicate()
        return CommandResult(
            exit_code=child.returncode or 0,
            stdout=output.decode(),
            stderr=error.decode(),
        )


class RecordingProvider:
    provider_name = "recording"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(request)
        yield LLMStart(
            request_id=request.request_id,
            created_at=datetime.now(UTC),
            model="recording",
            output_mode=request.output_mode,
        )


async def test_image_tool_reaches_provider_and_keeps_durable_bytes(
    tmp_path: Path,
) -> None:
    (tmp_path / "image.png").write_bytes(PNG)
    images = OfficeImages(
        LocalFixtureEnvironment(), str(tmp_path), evidence_dir=tmp_path / "evidence"
    )
    store = LocalAssetStore(project_root=tmp_path)
    async with aiohttp.ClientSession() as session:
        fetcher = SafeMediaFetcher(
            session=session, max_file_bytes=4194304, timeout_seconds=5
        )
        assets = AssetService(
            store=store,
            fetcher=fetcher,
            max_file_bytes=4194304,
            upload_timeout_seconds=5,
        )
        images.upload = assets.upload
        await images.read_image("image.png")
        services = PluginSetupServices()
        services.freeze()
        contributions = load_plugin_contributions(
            (images.plugin(),), agent_id="test", options={}, services=services
        )
        registry = MiddlewareRegistry()
        registry.register_many(contributions[0].middlewares)
        registry.freeze()
        ctx = SessionContext(
            app_id="test", agent_id="test", user_id="test", session_id="test"
        )
        decision = await MiddlewareManager(registry).dispatch(
            MiddlewareContext(
                invocation_id="image-test",
                point=MiddlewarePoint.BEFORE_TURN,
                ctx=ctx,
                run_id="run",
                trace_id="trace",
                state_snapshot=AgentState(session_context=ctx),
                payload=TurnMiddlewarePayload(messages=()),
            )
        )
        assert isinstance(decision.payload, MessagesMiddlewarePayload)
        block = decision.payload.messages[0].content[1]
        assert isinstance(block, ImageBlock) and isinstance(block.source, AssetSource)
        provider = RecordingProvider()
        client = LLMClient(
            LLMClientConfig(
                provider=provider,
                media_transport=MediaTransport.AUTO,
                asset_store=store,
                media_fetcher=fetcher,
                max_file_bytes=4194304,
            )
        )
        request = LLMRequest(
            request_id="request",
            messages=decision.payload.messages,
            config=LLMGenerationConfig(model="recording"),
            output_mode=OutputMode.COMPLETE,
        )
        async for _ in client.generate(request):
            pass
        image = provider.requests[0].messages[0].content[1]
        assert isinstance(image, ImageBlock) and isinstance(image.source, Base64Source)
        assert base64.b64decode(image.source.data) == PNG
        assert list((tmp_path / "evidence").iterdir())[0].read_bytes() == PNG
        assert not images.pending


async def test_image_tool_rejects_traversal_before_environment_io(
    tmp_path: Path,
) -> None:
    images = OfficeImages(LocalFixtureEnvironment(), str(tmp_path))
    with pytest.raises(ToolError, match="workspace-relative"):
        await images.read_image("../private.png")
