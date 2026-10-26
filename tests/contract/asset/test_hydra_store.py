"""公司 Hydra UAT 的可选真实 AssetStore 合约测试。"""

import hashlib
import os
from collections.abc import AsyncIterator
from typing import cast
from uuid import uuid4

import httpx
import pytest
from agentsystem.asset import AssetPutRequest
from agentsystem.asset.hydra_store import HydraAssetStore

type HydraSettings = tuple[str, str, str, str, str, str, str, str]


def _test_settings() -> HydraSettings | None:
    names = (
        "TEST_HYDRA_TOKEN_URL",
        "TEST_HYDRA_UPLOAD_URL",
        "TEST_HYDRA_FILE_PREFIX",
        "TEST_HYDRA_BUSINESS_TYPE",
        "TEST_HYDRA_BUSINESS_ID",
        "TEST_HYDRA_TEMPLATE_CODE",
        "TEST_HYDRA_ITEM_CODE",
        "TEST_HYDRA_CREATOR_ID",
    )
    values = tuple(os.environ.get(name) for name in names)
    if any(not value for value in values):
        return None
    return cast(HydraSettings, values)


@pytest.fixture
def hydra_settings() -> HydraSettings:
    """仅在显式配置 Hydra UAT 时启用会产生文件记录的真实合约。"""
    settings = _test_settings()
    if settings is None:
        pytest.skip("未配置隔离 TEST_HYDRA_* UAT 合约环境")
    return settings


async def _body(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def test_hydra_uat_put_open_round_trip(
    hydra_settings: HydraSettings,
) -> None:
    """真实上传微型无业务数据文件，并通过 opaque fileId 完整回读。"""
    (
        token_url,
        upload_url,
        file_prefix,
        business_type,
        business_id,
        template_code,
        item_code,
        creator_id,
    ) = hydra_settings
    marker = uuid4().hex
    content = f"agentsystem-hydra-uat-contract:{marker}\n".encode()
    filename = f"agentsystem-contract-{marker}.txt"
    async with httpx.AsyncClient() as client:
        store = HydraAssetStore(
            client=client,
            token_url=token_url,
            upload_url=upload_url,
            file_prefix=file_prefix,
            business_type=business_type,
            business_id=business_id,
            template_code=template_code,
            item_code=item_code,
            creator_id=creator_id,
            max_file_bytes=1024,
            timeout_seconds=30,
        )

        record = await store.put(
            AssetPutRequest(
                body=_body(content),
                media_type="text/plain",
                filename=filename,
            )
        )
        async with store.open(record.asset_id) as opened:
            downloaded = b"".join([chunk async for chunk in opened.body])

    assert record.filename == filename
    assert record.size_bytes == len(content)
    assert record.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert downloaded == content
