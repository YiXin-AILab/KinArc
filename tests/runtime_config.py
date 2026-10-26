"""跨测试层共享的显式 Runtime 素材配置。"""

from pathlib import Path

from agentsystem.config.models import AssetLimitsConfig, LocalAssetStoreConfig

TEST_ASSET_LIMITS = AssetLimitsConfig(max_file_bytes=16 * 1024 * 1024)
TEST_ASSET_STORE = LocalAssetStoreConfig(root=Path("data/assets"))
TEST_PROJECT_ROOT = Path(__file__).resolve().parents[1]

__all__ = ["TEST_ASSET_LIMITS", "TEST_ASSET_STORE", "TEST_PROJECT_ROOT"]
