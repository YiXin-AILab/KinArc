"""根据严格 Runtime 配置创建并托管唯一 AssetStore。"""

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session

from agentsystem.asset.hydra_store import HydraAssetStore
from agentsystem.asset.local_store import LocalAssetStore
from agentsystem.asset.protocol import AssetStore
from agentsystem.asset.s3_store import S3AssetStore
from agentsystem.config.models import (
    AssetLimitsConfig,
    HydraAssetStoreConfig,
    LocalAssetStoreConfig,
    RuntimeAssetStoreConfig,
    S3AssetStoreConfig,
)


@dataclass(frozen=True, slots=True)
class ManagedAssetStore:
    """把资产能力和其资源清理入口一并交给 Application。

    Attributes:
        store: 配置选中的唯一资产存储实现。
        close: 幂等异步关闭回调，释放工厂创建的 HTTP 或 S3 资源。
    """

    store: AssetStore
    close: Callable[[], Awaitable[None]]


async def create_asset_store(
    config: RuntimeAssetStoreConfig,
    *,
    project_root: Path,
    limits: AssetLimitsConfig,
) -> ManagedAssetStore:
    """创建配置指定的 Store，并把所有第三方资源收进统一生命周期。

    初始化失败会关闭已创建的资源；成功后由调用方执行返回的 close 回调。

    Args:
        config: 经校验的 Local、S3 或 Hydra 配置。
        project_root: 必须存在的项目根目录，用于解析本地存储路径。
        limits: 上传大小及第三方网络调用超时限制。

    Returns:
        存储实现及其统一清理入口。

    Raises:
        OSError: 项目根目录无法解析或资源初始化失败。
        ValueError: 具体 Store 的配置不合法。
    """
    root = project_root.resolve(strict=True)
    stack = AsyncExitStack()
    await stack.__aenter__()
    try:
        if isinstance(config, LocalAssetStoreConfig):
            store: AssetStore = LocalAssetStore(
                project_root=root,
                root=config.root,
            )
        elif isinstance(config, S3AssetStoreConfig):
            s3_client = await stack.enter_async_context(
                get_session().create_client(
                    "s3",
                    endpoint_url=config.endpoint_url,
                    region_name=config.region,
                    aws_access_key_id=config.access_key.get_secret_value(),
                    aws_secret_access_key=config.secret_key.get_secret_value(),
                    config=AioConfig(
                        connect_timeout=limits.upload_timeout_seconds,
                        read_timeout=limits.upload_timeout_seconds,
                    ),
                )
            )
            store = S3AssetStore(
                client=s3_client,
                bucket=config.bucket,
                multipart_part_bytes=config.multipart_part_bytes,
            )
        elif isinstance(config, HydraAssetStoreConfig):
            http_client = httpx.AsyncClient()
            stack.push_async_callback(http_client.aclose)
            store = HydraAssetStore(
                client=http_client,
                token_url=config.token_url,
                upload_url=config.upload_url,
                file_prefix=config.file_prefix,
                business_type=config.business_type,
                business_id=config.business_id,
                template_code=config.template_code,
                item_code=config.item_code,
                creator_id=config.creator_id,
                max_file_bytes=limits.max_file_bytes,
                timeout_seconds=limits.upload_timeout_seconds,
            )
        else:  # pragma: no cover - Pydantic discriminator 保证闭集
            raise TypeError("未知 AssetStore 配置")
    except BaseException:
        await stack.aclose()
        raise
    return ManagedAssetStore(store=store, close=_idempotent_close(stack))


def _idempotent_close(stack: AsyncExitStack) -> Callable[[], Awaitable[None]]:
    """把 ExitStack 关闭包装为显式幂等入口。"""
    is_closed = False

    async def close() -> None:
        """至多执行一次退出栈清理，释放工厂创建的全部资源。"""
        nonlocal is_closed
        if is_closed:
            return
        is_closed = True
        await stack.aclose()

    return close
