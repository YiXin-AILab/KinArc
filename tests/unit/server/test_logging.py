"""第三方 Server Logger 的统一配置测试。"""

import logging
from pathlib import Path

from agentsystem.common.logging import setup_logging
from agentsystem.config.models import RuntimeLoggingConfig


def test_setup_logging_routes_uvicorn_through_root(tmp_path: Path) -> None:
    """Uvicorn 自有 Handler 必须移除，避免与统一 Handler 重复输出。"""
    uvicorn_logger = logging.getLogger("uvicorn.access")
    original = logging.StreamHandler()
    uvicorn_logger.handlers = [original]
    uvicorn_logger.propagate = False

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    try:
        assert uvicorn_logger.handlers == []
        assert uvicorn_logger.propagate is True
    finally:
        runtime.close()


def test_setup_logging_routes_litellm_through_root(tmp_path: Path) -> None:
    """LiteLLM 自有 Handler 必须移除，避免同一模型日志重复输出。"""
    litellm_logger = logging.getLogger("LiteLLM")
    original = logging.StreamHandler()
    litellm_logger.handlers = [original]
    litellm_logger.propagate = False

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    try:
        assert litellm_logger.handlers == []
        assert litellm_logger.propagate is True
    finally:
        runtime.close()
