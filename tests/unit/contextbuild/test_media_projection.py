"""模型侧 Asset 句柄投影测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.context.builder import project_asset_references
from agentsystem.message.models import (
    AssetSource,
    AudioBlock,
    Base64Source,
    DocumentBlock,
    ImageBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    UrlSource,
    VideoBlock,
)

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _message(*content: object) -> Message:
    """构造固定 USER Message。"""
    return Message.model_validate(
        {
            "message_id": "media",
            "session_id": "session",
            "role": Role.USER,
            "content": content,
            "origin": MessageOrigin.USER,
            "visibility": MessageVisibility.ALL,
            "run_id": "run",
            "created_at": _NOW,
        }
    )


@pytest.mark.parametrize(
    ("block", "expected"),
    (
        (
            ImageBlock(source=AssetSource(asset_id="image-id", media_type="image/png")),
            '<asset_reference asset_id="image-id" type="image" '
            'media_type="image/png" />',
        ),
        (
            AudioBlock(
                source=AssetSource(asset_id="audio-id", media_type="audio/mpeg")
            ),
            '<asset_reference asset_id="audio-id" type="audio" '
            'media_type="audio/mpeg" />',
        ),
        (
            VideoBlock(source=AssetSource(asset_id="video-id", media_type="video/mp4")),
            '<asset_reference asset_id="video-id" type="video" '
            'media_type="video/mp4" />',
        ),
        (
            DocumentBlock(
                source=AssetSource(
                    asset_id="document-id",
                    media_type="application/pdf",
                )
            ),
            '<asset_reference asset_id="document-id" type="document" '
            'media_type="application/pdf" />',
        ),
    ),
)
def test_projection_inserts_adjacent_reference_for_each_asset_media(
    block: object,
    expected: str,
) -> None:
    """漏掉任一 Asset 媒体类型都会让模型无法为 Tool 复制句柄。"""
    source = _message(TextBlock(text="before"), block, TextBlock(text="after"))

    projected = project_asset_references(source, enabled=True)

    assert projected.content[0] == TextBlock(text="before")
    assert projected.content[1] == TextBlock(text=expected)
    assert projected.content[2] == block
    assert projected.content[3] == TextBlock(text="after")
    assert source.content == (TextBlock(text="before"), block, TextBlock(text="after"))


def test_projection_escapes_runtime_generated_xml_attributes() -> None:
    """特殊字符不得破坏 Runtime 生成的引用边界。"""
    media = ImageBlock(
        source=AssetSource(
            asset_id="asset<&\"'",
            media_type='image/png&profile="safe"',
            filename="must-not-appear.png",
        )
    )

    projected = project_asset_references(_message(media), enabled=True)

    assert projected.content[0] == TextBlock(
        text=(
            '<asset_reference asset_id="asset&lt;&amp;&quot;&#x27;" type="image" '
            'media_type="image/png&amp;profile=&quot;safe&quot;" />'
        )
    )
    assert "must-not-appear" not in projected.content[0].text


def test_projection_leaves_external_media_and_disabled_messages_unchanged() -> None:
    """URL/Base64 或关闭配置时不得产生不存在的 Runtime 句柄。"""
    external = _message(
        ImageBlock(
            source=UrlSource(url="https://example.test/a.png", media_type="image/png")
        ),
        AudioBlock(source=Base64Source(data="YQ==", media_type="audio/mpeg")),
    )
    asset = _message(
        DocumentBlock(
            source=AssetSource(asset_id="document-id", media_type="application/pdf")
        )
    )

    assert project_asset_references(external, enabled=True) == external
    assert project_asset_references(asset, enabled=False) == asset


def test_projection_does_not_duplicate_an_existing_adjacent_reference() -> None:
    """同一发送副本经过两个 Context 边界时不得累积重复句柄。"""
    source = _message(
        DocumentBlock(
            source=AssetSource(asset_id="document-id", media_type="application/pdf")
        )
    )

    first = project_asset_references(source, enabled=True)
    second = project_asset_references(first, enabled=True)

    assert second == first
    assert len(second.content) == 2
