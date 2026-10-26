"""多模态输入从 HTTP 到 Redis、Context 与 LiteLLM 的完整链路测试。"""

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agentsystem.asset.local_store import LocalAssetStore
from agentsystem.asset.models import AssetPutRequest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentMediaConfig,
    AgentOutboundConfig,
    AssetLimitsConfig,
    DispatcherConfig,
    LocalAssetStoreConfig,
    MediaArchiveRuleConfig,
    ModelParamsConfig,
    RuntimeConfig,
)
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.models import MediaTransport
from agentsystem.message.models import (
    AssetSource,
    ImageBlock,
    MessageVisibility,
    Role,
    UrlSource,
)
from agentsystem.message.redis.manager import RedisMessageManager
from agentsystem.redis.client import (
    RedisClient,
    close_redis_client,
    create_redis_client,
)
from agentsystem.runtime.state import SessionStatus
from agentsystem.service import host as service_module
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from httpx import ASGITransport, AsyncClient
from litellm import ModelResponseStream
from server.app import create_runtime_api_app
from tests.redis_config import local_redis_config
from tests.unit.llm.stream_helpers import text_stream

_PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452")
_WAV = (
    b"RIFF"
    + (36).to_bytes(4, "little")
    + b"WAVEfmt "
    + (16).to_bytes(4, "little")
    + (1).to_bytes(2, "little")
    + (1).to_bytes(2, "little")
    + (8000).to_bytes(4, "little")
    + (8000).to_bytes(4, "little")
    + (1).to_bytes(2, "little")
    + (8).to_bytes(2, "little")
    + b"data"
    + (0).to_bytes(4, "little")
)
_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
_PDF = b"%PDF-1.4\n%%EOF\n"


class _MediaFetcher:
    """记录 LLM base64 策略解析的 URL 并返回固定媒体。"""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.urls: list[str] = []

    async def fetch(self, url: str) -> AsyncIterator[bytes]:
        """返回测试正文。"""
        self.urls.append(url)
        yield self._body


async def test_uploaded_asset_reaches_agent_and_can_be_read_back(
    tmp_path: Path,
) -> None:
    """公开上传返回的 source 必须可直接驱动 Agent 并供前端完整回显。"""
    prefix = f"multimodal-upload-{uuid4().hex}"
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        requests.append(kwargs)
        return text_stream("uploaded-ok", response_id="upload-response")

    redis = await create_redis_client(local_redis_config())
    service = await _create_service(
        tmp_path=tmp_path,
        prefix=prefix,
        redis=redis,
        provider=LiteLLMProvider(
            completion=completion,
        ),
    )
    app = create_runtime_api_app(service)
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
    try:
        await service.start()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            uploaded = await client.post(
                "/v1/assets",
                files={"file": ("demo.png", _PNG, "image/png")},
            )
            assert uploaded.status_code == 201
            payload = uploaded.json()

            submitted = await client.post(
                "/v1/sessions/upload-session/messages",
                json={
                    **identity,
                    "message_id": "upload-message",
                    "content": [
                        {"type": "image", "source": payload["source"]},
                        {"type": "text", "text": "inspect upload"},
                    ],
                },
            )
            assert submitted.status_code == 202
            await _wait_terminal(service, "upload-session", SessionStatus.SUCCEEDED)

            downloaded = await client.get(f"/v1/assets/{payload['asset_id']}")
            public = await client.get(
                "/v1/sessions/upload-session/messages",
                params=identity,
            )

        assert downloaded.content == _PNG
        assert downloaded.headers["content-type"] == "image/png"
        assert requests[0]["messages"][-1]["content"][0]["image_url"]["url"].endswith(
            base64.b64encode(_PNG).decode("ascii")
        )
        blocks = public.json()["messages"][0]["blocks"]
        assert [block["type"] for block in blocks] == ["image", "text"]
        assert blocks[0]["source_type"] == "asset"
        assert blocks[0]["asset_id"] == payload["asset_id"]
    finally:
        await service.close()
        await close_redis_client(redis)


async def test_asset_handle_is_model_only_when_agent_enables_context_projection(
    tmp_path: Path,
) -> None:
    """开启后模型同时看到句柄和正文，权威消息仍只有原始媒体 Block。"""
    prefix = f"multimodal-handle-{uuid4().hex}"
    store = LocalAssetStore(project_root=tmp_path)
    asset = await store.put(
        AssetPutRequest(body=_body(_PNG), media_type="image/png", filename="demo.png")
    )
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        requests.append(kwargs)
        return text_stream("handled", response_id="handle-response")

    redis = await create_redis_client(local_redis_config())
    service = await _create_service(
        tmp_path=tmp_path,
        prefix=prefix,
        redis=redis,
        provider=LiteLLMProvider(completion=completion),
        media=AgentMediaConfig(include_asset_id_in_context=True),
    )
    app = create_runtime_api_app(service)
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
    try:
        await service.start()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            response = await client.post(
                "/v1/sessions/handle-session/messages",
                json={
                    **identity,
                    "message_id": "handle-message",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "asset",
                                "asset_id": asset.asset_id,
                                "media_type": "image/png",
                                "filename": "demo.png",
                            },
                        }
                    ],
                },
            )
        assert response.status_code == 202
        await _wait_terminal(service, "handle-session", SessionStatus.SUCCEEDED)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            public = await client.get(
                "/v1/sessions/handle-session/messages",
                params=identity,
            )
        assert public.status_code == 200
        assert "asset_reference" not in public.text

        provider_content = requests[0]["messages"][-1]["content"]
        assert provider_content[0] == {
            "type": "text",
            "text": (
                f'<asset_reference asset_id="{asset.asset_id}" type="image" '
                'media_type="image/png" />'
            ),
        }
        assert provider_content[1]["image_url"]["url"].endswith(
            base64.b64encode(_PNG).decode("ascii")
        )

        ctx = SessionContext(**identity, session_id="handle-session")
        messages = await RedisMessageManager(redis, prefix=prefix).list_messages(ctx)
        user = next(message for message in messages if message.role is Role.USER)
        assert user.content == (
            ImageBlock(
                source=AssetSource(
                    asset_id=asset.asset_id,
                    media_type="image/png",
                    filename="demo.png",
                )
            ),
        )
    finally:
        await service.close()
        await close_redis_client(redis)


async def test_all_media_blocks_reach_litellm_and_public_views_are_redacted(
    tmp_path: Path,
) -> None:
    """四类 Block 保序进入 LiteLLM，仅 Asset 公开回读 ID。"""
    prefix = f"multimodal-e2e-{uuid4().hex}"
    store = LocalAssetStore(project_root=tmp_path)
    pdf = await store.put(
        AssetPutRequest(
            body=_body(_PDF),
            media_type="application/pdf",
            filename="report.pdf",
        )
    )
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        requests.append(kwargs)
        return text_stream("multimodal-ok", response_id="media-response")

    redis = await create_redis_client(local_redis_config())
    service = await _create_service(
        tmp_path=tmp_path,
        prefix=prefix,
        redis=redis,
        provider=LiteLLMProvider(
            completion=completion,
        ),
    )
    app = create_runtime_api_app(service)
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
    image_url = "https://cdn.example.test/image.png?private=secret"
    audio_data = base64.b64encode(_WAV).decode("ascii")
    video_data = base64.b64encode(_MP4).decode("ascii")
    try:
        await service.start()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            response = await client.post(
                "/v1/sessions/media-session/messages",
                json={
                    **identity,
                    "message_id": "media-message",
                    "content": [
                        {"type": "text", "text": "inspect all"},
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": image_url,
                                "media_type": "image/png",
                            },
                            "detail": "high",
                        },
                        {
                            "type": "audio",
                            "source": {
                                "type": "base64",
                                "data": audio_data,
                                "media_type": "audio/wav",
                            },
                        },
                        {
                            "type": "video",
                            "source": {
                                "type": "base64",
                                "data": video_data,
                                "media_type": "video/mp4",
                            },
                        },
                        {
                            "type": "document",
                            "source": {
                                "type": "asset",
                                "asset_id": pdf.asset_id,
                                "media_type": "application/pdf",
                                "filename": "report.pdf",
                            },
                        },
                    ],
                },
            )
            assert response.status_code == 202
            await _wait_terminal(service, "media-session", SessionStatus.SUCCEEDED)

            public = await client.get(
                "/v1/sessions/media-session/messages",
                params=identity,
            )
            assert public.status_code == 200
            assert "private=secret" not in public.text
            assert audio_data not in public.text
            assert video_data not in public.text
            public_blocks = public.json()["messages"][0]["blocks"]
            assert "asset_id" not in public_blocks[1]
            assert "asset_id" not in public_blocks[2]
            assert "asset_id" not in public_blocks[3]
            assert public_blocks[4]["asset_id"] == pdf.asset_id

        user_content = requests[0]["messages"][-1]["content"]
        assert [item["type"] for item in user_content] == [
            "text",
            "image_url",
            "input_audio",
            "video_url",
            "image_url",
        ]
        assert user_content[1]["image_url"] == {
            "url": image_url,
            "detail": "high",
        }
        assert user_content[2]["input_audio"]["data"] == audio_data
        assert user_content[3]["video_url"]["url"].endswith(video_data)
        assert user_content[4]["image_url"]["url"].endswith(
            base64.b64encode(_PDF).decode("ascii")
        )
    finally:
        await service.close()
        await close_redis_client(redis)


async def test_base64_transport_converts_url_without_rewriting_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent 的 base64 策略只改变 Provider 请求，不改变消息权威内容。"""
    prefix = f"multimodal-base64-{uuid4().hex}"
    fetcher = _MediaFetcher(_PNG)
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        requests.append(kwargs)
        return text_stream("converted", response_id="base64-response")

    redis = await create_redis_client(local_redis_config())
    monkeypatch.setattr(
        service_module,
        "SafeMediaFetcher",
        lambda **_: fetcher,
    )
    service = await _create_service(
        tmp_path=tmp_path,
        prefix=prefix,
        redis=redis,
        provider=LiteLLMProvider(
            completion=completion,
        ),
        media=AgentMediaConfig(llm_transport=MediaTransport.BASE64),
    )
    app = create_runtime_api_app(service)
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
    image_url = "https://cdn.example.test/image.png"
    try:
        await service.start()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            response = await client.post(
                "/v1/sessions/base64-session/messages",
                json={
                    **identity,
                    "message_id": "base64-message",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": image_url,
                                "media_type": "image/png",
                            },
                        }
                    ],
                },
            )
            assert response.status_code == 202
            await _wait_terminal(service, "base64-session", SessionStatus.SUCCEEDED)

        provider_url = requests[0]["messages"][-1]["content"][0]["image_url"]["url"]
        assert provider_url == (
            "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
        )
        assert fetcher.urls == [image_url]

        ctx = SessionContext(**identity, session_id="base64-session")
        messages = await RedisMessageManager(redis, prefix=prefix).list_messages(ctx)
        user = next(message for message in messages if message.role is Role.USER)
        assert isinstance(user.content[0], ImageBlock)
        assert isinstance(user.content[0].source, UrlSource)
        assert user.content[0].source.url == image_url
    finally:
        await service.close()
        await close_redis_client(redis)


async def test_matching_archive_rule_persists_asset_source(
    tmp_path: Path,
) -> None:
    """匹配规则把 Base64 替换为 AssetSource 后再写入 MessageManager。"""
    prefix = f"multimodal-archive-{uuid4().hex}"

    async def completion(**_: Any) -> AsyncIterator[ModelResponseStream]:
        return text_stream("archived", response_id="archive-response")

    redis = await create_redis_client(local_redis_config())
    service = await _create_service(
        tmp_path=tmp_path,
        prefix=prefix,
        redis=redis,
        provider=LiteLLMProvider(
            completion=completion,
        ),
        media=AgentMediaConfig(
            archive_rules=(
                MediaArchiveRuleConfig(
                    block_types=("image",),
                    source_types=("base64",),
                ),
            )
        ),
    )
    app = create_runtime_api_app(service)
    image_data = base64.b64encode(_PNG).decode("ascii")
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
    try:
        await service.start()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            response = await client.post(
                "/v1/sessions/archive-session/messages",
                json={
                    **identity,
                    "message_id": "archive-message",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "data": image_data,
                                "media_type": "image/png",
                            },
                        }
                    ],
                },
            )
            assert response.status_code == 202
            await _wait_terminal(service, "archive-session", SessionStatus.SUCCEEDED)

        ctx = SessionContext(**identity, session_id="archive-session")
        messages = await RedisMessageManager(redis, prefix=prefix).list_messages(ctx)
        user = next(message for message in messages if message.role is Role.USER)
        assert isinstance(user.content[0], ImageBlock)
        assert isinstance(user.content[0].source, AssetSource)
    finally:
        await service.close()
        await close_redis_client(redis)


async def test_missing_asset_fails_with_stable_code_after_saving_user_message(
    tmp_path: Path,
) -> None:
    """运行期素材解析失败保存 USER 边界，并以稳定错误进入终态。"""
    prefix = f"multimodal-missing-{uuid4().hex}"

    async def completion(**_: Any) -> AsyncIterator[ModelResponseStream]:
        raise AssertionError("素材解析失败时不应调用模型")

    redis = await create_redis_client(local_redis_config())
    service = await _create_service(
        tmp_path=tmp_path,
        prefix=prefix,
        redis=redis,
        provider=LiteLLMProvider(
            completion=completion,
        ),
    )
    app = create_runtime_api_app(service)
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
    try:
        await service.start()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            response = await client.post(
                "/v1/sessions/missing-session/messages",
                json={
                    **identity,
                    "message_id": "missing-message",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "asset",
                                "asset_id": "sha256-" + "d" * 64,
                                "media_type": "image/png",
                            },
                        }
                    ],
                },
            )
            assert response.status_code == 202
            await _wait_terminal(service, "missing-session", SessionStatus.FAILED)
            snapshot = await client.get(
                "/v1/sessions/missing-session",
                params=identity,
            )
            assert snapshot.json()["result"]["error_code"] == "asset_not_found"

        ctx = SessionContext(**identity, session_id="missing-session")
        messages = await RedisMessageManager(redis, prefix=prefix).list_messages(ctx)
        assert [message.role for message in messages] == [
            Role.SYSTEM,
            Role.USER,
            Role.USER,
        ]
        assert [message.visibility for message in messages] == [
            MessageVisibility.MODEL,
            MessageVisibility.ALL,
            MessageVisibility.MODEL,
        ]
    finally:
        await service.close()
        await close_redis_client(redis)


async def _create_service(
    *,
    tmp_path: Path,
    prefix: str,
    redis: RedisClient,
    provider: LiteLLMProvider,
    media: AgentMediaConfig | None = None,
) -> ServiceHost:
    config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        asset=AssetLimitsConfig(max_file_bytes=1024 * 1024),
        asset_store=LocalAssetStoreConfig(),
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
    )
    return await ServiceHost.create(
        runtime_config=config,
        project_root=tmp_path,
        agents=(
            AgentRuntimeSpec(
                agent_config=AgentConfig(
                    agent_id="agent",
                    agent_name="Multimodal Agent",
                    prompt="unused.md",
                    outbound=AgentOutboundConfig(
                        type="log",
                    ),
                    llm_model=ModelParamsConfig(model="openai/test-model"),
                    media=media or AgentMediaConfig(),
                ),
                base_prompt="你是多模态测试助手。",
                llm_provider=provider,
            ),
        ),
        redis_client=redis,
        consumer_id="multimodal-worker",
    )


async def _wait_terminal(
    service: ServiceHost,
    session_id: str,
    expected: SessionStatus,
) -> None:
    ctx = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id=session_id,
    )
    async with asyncio.timeout(3):
        while True:
            snapshot = await service.application.snapshot(ctx)
            if snapshot is not None and snapshot.status is expected:
                return
            await asyncio.sleep(0.01)


async def _body(value: bytes) -> AsyncIterator[bytes]:
    yield value
