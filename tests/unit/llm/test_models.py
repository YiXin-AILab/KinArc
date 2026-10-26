"""LLM 公共模型与统一事件协议的行为测试。"""

import pytest
from agentsystem.llm.models import (
    LLMGenerationConfig,
    Usage,
    UsageAccounting,
)
from pydantic import ValidationError


def test_exact_usage_requires_matching_total() -> None:
    """精确计费时，总 token 数必须等于输入与输出之和。"""
    with pytest.raises(ValidationError):
        Usage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=99,
            accounting=UsageAccounting.EXACT,
        )


def test_partial_usage_preserves_provider_total() -> None:
    """部分计费时必须保留提供方返回的总 token 数。"""
    usage = Usage(input_tokens=10, output_tokens=5, total_tokens=20)

    assert usage.total_tokens == 20


def test_generation_config_rejects_media_transport() -> None:
    """媒体传输是 Client 准备策略，不得混入模型生成参数。"""
    with pytest.raises(ValidationError):
        LLMGenerationConfig.model_validate(
            {"model": "openai/test", "media_transport": "base64"}
        )
