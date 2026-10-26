"""AssetStore 的 Provider 无关异步协议。"""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from agentsystem.asset.models import AssetContent, AssetPutRequest, AssetRecord


class AssetStore(Protocol):
    """Runtime 全局选择的资产持久化边界。

    实现负责将存储细节收敛到领域记录；连接生命周期由 Application 管理，
    单次读取资源由 open 返回的上下文管理。协议本身不执行媒体类型检测。
    """

    async def put(self, request: AssetPutRequest) -> AssetRecord:
        """消费正文并保存资产。

        Args:
            request: 正文流及其声明元数据。

        Returns:
            可用于后续读取的资产标识和实际大小、摘要。
        """
        ...

    def open(self, asset_id: str) -> AbstractAsyncContextManager[AssetContent]:
        """以异步上下文管理器打开资产正文。

        Args:
            asset_id: 当前 Store 能解释的资产标识。

        Returns:
            提供 MIME 与正文流的上下文；调用方必须在上下文内消费正文。
        """
        ...
