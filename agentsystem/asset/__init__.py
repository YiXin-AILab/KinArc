"""Runtime 资产存储的稳定公开接口。"""

from agentsystem.asset.factory import ManagedAssetStore, create_asset_store
from agentsystem.asset.hydra_store import HydraAssetStore
from agentsystem.asset.local_store import LocalAssetStore
from agentsystem.asset.models import (
    AssetContent,
    AssetPutRequest,
    AssetRecord,
)
from agentsystem.asset.protocol import AssetStore
from agentsystem.asset.s3_store import S3AssetStore

__all__ = [
    "AssetContent",
    "AssetPutRequest",
    "AssetRecord",
    "AssetStore",
    "HydraAssetStore",
    "LocalAssetStore",
    "ManagedAssetStore",
    "S3AssetStore",
    "create_asset_store",
]
