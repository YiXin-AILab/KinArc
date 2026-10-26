"""真实 S3/OSS compatible 服务的 AssetStore 合约测试。"""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from agentsystem.asset import AssetPutRequest
from agentsystem.asset.s3_store import S3AssetStore
from agentsystem.common.errors import AdapterError
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session

type S3Settings = tuple[str, str, str, str, str]
_MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024


def _test_settings() -> S3Settings | None:
    names = (
        "TEST_ASSET_S3_ENDPOINT",
        "TEST_ASSET_S3_BUCKET",
        "TEST_ASSET_S3_REGION",
        "TEST_ASSET_S3_ACCESS_KEY",
        "TEST_ASSET_S3_SECRET_KEY",
    )
    values = tuple(os.environ.get(name) for name in names)
    if any(value is None for value in values):
        return None
    return cast(S3Settings, values)


@pytest.fixture
def s3_settings() -> S3Settings:
    """读取显式隔离环境；未配置时清晰跳过真实合约。"""
    settings = _test_settings()
    if settings is None:
        pytest.skip("未配置隔离 TEST_ASSET_S3_* 合约环境")
    return settings


@asynccontextmanager
async def _client(
    settings: S3Settings,
    *,
    max_pool_connections: int = 10,
) -> AsyncIterator[Any]:
    endpoint, _, region, access_key, secret_key = settings
    async with get_session().create_client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=AioConfig(
            max_pool_connections=max_pool_connections,
            connect_timeout=2,
            read_timeout=5,
        ),
    ) as client:
        yield client


async def _body(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def _collect(body: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in body])


def _request(value: bytes) -> AssetPutRequest:
    return AssetPutRequest(
        body=_body(value),
        media_type="application/octet-stream",
    )


def _key(value: bytes) -> str:
    digest = hashlib.sha256(value).hexdigest()
    return f"sha256/{digest[:2]}/{digest}"


async def test_real_s3_content_addressing_deduplicates_repeated_uploads(
    s3_settings: S3Settings,
) -> None:
    """删除 exists 检查会使真实重复上传不再只有一个确定性对象。"""
    _, bucket, _, _, _ = s3_settings
    content = os.urandom(64)
    key = _key(content)
    async with _client(s3_settings) as client:
        store = S3AssetStore(client=client, bucket=bucket)
        try:
            first = await store.put(_request(content))
            second = await store.put(_request(content))
            listed = await client.list_objects_v2(Bucket=bucket, Prefix=key)

            assert first.asset_id == second.asset_id
            assert [item["Key"] for item in listed.get("Contents", [])] == [key]
            async with store.open(first.asset_id) as opened:
                assert await _collect(opened.body) == content
        finally:
            await client.delete_object(Bucket=bucket, Key=key)


async def test_real_s3_multipart_upload_round_trips_large_content(
    s3_settings: S3Settings,
) -> None:
    """退化为单请求上传会失去 multipart ETag，并由真实 MinIO 合约捕获。"""
    _, bucket, _, _, _ = s3_settings
    content = os.urandom(_MIN_MULTIPART_PART_BYTES + 257)
    key = _key(content)
    async with _client(s3_settings) as client:
        store = S3AssetStore(
            client=client,
            bucket=bucket,
            multipart_part_bytes=_MIN_MULTIPART_PART_BYTES,
        )
        try:
            record = await store.put(_request(content))
            metadata = await client.head_object(Bucket=bucket, Key=key)

            assert metadata["ContentLength"] == len(content)
            assert str(metadata["ETag"]).strip('"').endswith("-2")
            async with store.open(record.asset_id) as opened:
                assert await _collect(opened.body) == content
        finally:
            await client.delete_object(Bucket=bucket, Key=key)


async def test_real_s3_early_reader_exit_returns_pool_connection(
    s3_settings: S3Settings,
) -> None:
    """删除 StreamingBody 关闭会耗尽单连接池，使后续部分读取超时。"""
    _, bucket, _, _, _ = s3_settings
    content = os.urandom(1024 * 1024)
    key = _key(content)
    async with _client(s3_settings, max_pool_connections=1) as client:
        store = S3AssetStore(client=client, bucket=bucket)
        try:
            record = await store.put(_request(content))
            async with asyncio.timeout(3):
                for _ in range(3):
                    async with store.open(record.asset_id) as opened:
                        async for chunk in opened.body:
                            assert chunk
                            break
                async with store.open(record.asset_id) as opened:
                    assert await _collect(opened.body) == content
        finally:
            await client.delete_object(Bucket=bucket, Key=key)


async def test_real_s3_missing_object_maps_to_stable_error(
    s3_settings: S3Settings,
) -> None:
    """真实 NoSuchKey 必须收敛为不重试且不泄露 SDK 的 asset_not_found。"""
    _, bucket, _, _, _ = s3_settings
    missing_id = "sha256-" + hashlib.sha256(os.urandom(64)).hexdigest()
    async with _client(s3_settings) as client:
        store = S3AssetStore(client=client, bucket=bucket)

        with pytest.raises(AdapterError) as captured:
            async with store.open(missing_id):
                pass

    assert captured.value.code == "asset_not_found"
    assert captured.value.retryable is False
