"""AssetStore 领域记录与上传请求。"""

import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass

from pydantic import Field, field_validator

from agentsystem.common.models import StrictModel

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
LOCAL_ASSET_ID_PATTERN = re.compile(r"sha256-([0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class AssetPutRequest:
    """一次单次消费的流式资产写入请求，不持有存储连接。

    大小上限由调用方或具体 Store 执行，本记录只校验元数据格式。

    Attributes:
        body: 待消费的字节流。
        media_type: 调用方声明的 MIME 类型。
        filename: 可选展示文件名，不作为本地存储路径。
        expected_size: 可选预期字节数，由支持完整性校验的 Store 比对。
        expected_sha256: 可选的小写 SHA-256 十六进制摘要。
    """

    body: AsyncIterable[bytes]
    media_type: str
    filename: str | None = None
    expected_size: int | None = None
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        """拒绝无法成立的调用方元数据。"""
        if not self.media_type:
            raise ValueError("media_type 不能为空")
        if self.expected_size is not None and self.expected_size <= 0:
            raise ValueError("expected_size 必须为正数")
        if (
            self.expected_sha256 is not None
            and SHA256_PATTERN.fullmatch(self.expected_sha256) is None
        ):
            raise ValueError("expected_sha256 格式无效")


class AssetRecord(StrictModel):
    """AssetStore 写入后返回的 Provider 无关元数据。

    Attributes:
        asset_id: 由 Store 解释的非空不透明标识，可包含路径段。
        media_type: 写入时的 MIME 类型。
        filename: 可选展示文件名。
        size_bytes: 实际写入的正字节数。
        checksum_sha256: 实际正文的 SHA-256 摘要。
    """

    asset_id: str = Field(min_length=1, max_length=2048)
    media_type: str = Field(min_length=1)
    filename: str | None = None
    size_bytes: int = Field(gt=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("asset_id")
    @classmethod
    def validate_opaque_asset_id(cls, value: str) -> str:
        """允许 Provider 路径型 ID，但拒绝控制字符进入持久化边界。

        Args:
            value: 待校验的资产标识。

        Returns:
            原始标识，不执行路径归一化。

        Raises:
            ValueError: 标识包含控制字符。
        """
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("asset_id 包含控制字符")
        return value


@dataclass(frozen=True, slots=True)
class AssetContent:
    """一次打开资产得到的 MIME 元数据与单次异步正文流。

    Store 返回的 MIME 不一定经过内容检测；AssetService 可进一步验证。
    正文只能在创建它的异步上下文内消费，退出上下文会关闭底层 reader。

    Attributes:
        media_type: 存储返回或服务检测得到的 MIME。
        body: 单次消费的异步字节迭代器。
    """

    media_type: str
    body: AsyncIterator[bytes]
