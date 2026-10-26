"""Runtime 服务日志的上下文、输出与滚动行为测试。"""

import asyncio
import io
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agentsystem.common import logging as common_logging
from agentsystem.common.errors import RunFailure
from agentsystem.common.logging import (
    DateSizeRotatingFileHandler,
    bind_log_context,
    log_unexpected_exception,
    setup_logging,
)
from agentsystem.config.models import RuntimeLoggingConfig


def _logger() -> logging.Logger:
    logger = logging.getLogger("tests.runtime.logging")
    logger.setLevel(logging.DEBUG)
    return logger


def test_setup_logging_routes_levels_and_writes_all_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制台级别必须互斥，文件必须包含全部已启用记录。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    runtime = setup_logging(
        RuntimeLoggingConfig(
            level="DEBUG",
            directory=Path("logs"),
            max_bytes=1_024,
        ),
        project_root=tmp_path,
    )
    logger = _logger()

    try:
        logger.debug("debug record")
        logger.info("info record")
        logger.warning("warning record")
    finally:
        runtime.close()

    assert "debug record" in stdout.getvalue()
    assert "info record" in stdout.getvalue()
    assert "warning record" not in stdout.getvalue()
    assert "debug record" not in stderr.getvalue()
    assert "info record" not in stderr.getvalue()
    assert "warning record" in stderr.getvalue()
    file_text = (tmp_path / "logs" / "agentsystem.log").read_text()
    assert "debug record" in file_text
    assert "info record" in file_text
    assert "warning record" in file_text


def test_bind_log_context_nests_and_restores_fields(
    tmp_path: Path,
) -> None:
    """内层字段覆盖后必须恢复外层值，离开作用域后不得残留。"""
    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    logger = _logger()

    try:
        with bind_log_context(session_id="outer", run_id="run-1"):
            logger.info("outer record")
            with bind_log_context(session_id="inner"):
                logger.info("inner record")
            logger.info("restored record")
        logger.info("outside record")
    finally:
        runtime.close()

    lines = (tmp_path / "logs" / "agentsystem.log").read_text().splitlines()
    outer, inner, restored, outside = lines
    assert "session=outer run=run-1" in outer
    assert "session=inner run=run-1" in inner
    assert "session=outer run=run-1" in restored
    assert "session=" not in outside
    assert "run=" not in outside


def test_log_format_renders_only_bound_context_fields(tmp_path: Path) -> None:
    """基础设施日志不得显示空业务字段，业务日志只显示已绑定字段。"""
    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    logger = _logger()

    try:
        logger.info("infrastructure record")
        with bind_log_context(app_id="app-1", session_id="session-1"):
            logger.info("business record")
    finally:
        runtime.close()

    infrastructure, business = (
        (tmp_path / "logs" / "agentsystem.log").read_text().splitlines()
    )
    assert "request=" not in infrastructure
    assert "app=" not in infrastructure
    assert "session=" not in infrastructure
    assert "app=app-1 session=session-1" in business
    assert "user=" not in business
    assert "run=" not in business


async def test_bind_log_context_isolates_concurrent_tasks(tmp_path: Path) -> None:
    """两个异步任务的关联字段不能互相串线。"""
    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    logger = _logger()
    both_bound = asyncio.Event()
    bound_count = 0
    count_lock = asyncio.Lock()

    async def write(session_id: str) -> None:
        nonlocal bound_count
        with bind_log_context(session_id=session_id):
            async with count_lock:
                bound_count += 1
                if bound_count == 2:
                    both_bound.set()
            await both_bound.wait()
            logger.info("task %s", session_id)

    try:
        await asyncio.gather(write("session-a"), write("session-b"))
    finally:
        runtime.close()

    lines = (tmp_path / "logs" / "agentsystem.log").read_text().splitlines()
    assert any(
        "session=session-a" in line and "task session-a" in line for line in lines
    )
    assert any(
        "session=session-b" in line and "task session-b" in line for line in lines
    )
    assert not any(
        "session=session-a" in line and "task session-b" in line for line in lines
    )
    assert not any(
        "session=session-b" in line and "task session-a" in line for line in lines
    )


def test_file_handler_rolls_over_before_size_limit_is_exceeded(
    tmp_path: Path,
) -> None:
    """下一条记录超过大小上限时，已有内容应进入当日首个归档。"""
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    active = tmp_path / "agentsystem.log"
    handler = DateSizeRotatingFileHandler(
        active,
        max_bytes=15,
        retention_days=14,
        max_backup_files=100,
        clock=lambda: now,
    )
    logger = logging.getLogger("tests.runtime.logging.size")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        logger.info("1234567890")
        logger.info("abcdefghij")
    finally:
        handler.close()
        logger.handlers.clear()
        logger.propagate = True

    assert (tmp_path / "agentsystem.2026-08-13.001.log").read_text() == ("1234567890\n")
    assert active.read_text() == "abcdefghij\n"


def test_file_handler_rolls_over_when_local_date_changes(tmp_path: Path) -> None:
    """自然日变化时即使文件很小也必须归档上一日内容。"""
    current = [datetime(2026, 8, 13, 23, 59, tzinfo=UTC)]
    active = tmp_path / "agentsystem.log"
    handler = DateSizeRotatingFileHandler(
        active,
        max_bytes=1_024,
        retention_days=14,
        max_backup_files=100,
        clock=lambda: current[0],
    )
    logger = logging.getLogger("tests.runtime.logging.date")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        logger.info("day one")
        current[0] = datetime(2026, 8, 14, 0, 1, tzinfo=UTC)
        logger.info("day two")
    finally:
        handler.close()
        logger.handlers.clear()
        logger.propagate = True

    assert (tmp_path / "agentsystem.2026-08-13.001.log").read_text() == "day one\n"
    assert active.read_text() == "day two\n"


def test_empty_file_adopts_new_date_without_delayed_rollover(tmp_path: Path) -> None:
    """空活动文件跨日后首条记录应直接属于新日期。"""
    current = [datetime(2026, 8, 13, 23, 59, tzinfo=UTC)]
    active = tmp_path / "agentsystem.log"
    handler = DateSizeRotatingFileHandler(
        active,
        max_bytes=1_024,
        retention_days=14,
        max_backup_files=100,
        clock=lambda: current[0],
    )
    logger = logging.getLogger("tests.runtime.logging.empty-date")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        current[0] = datetime(2026, 8, 14, 0, 1, tzinfo=UTC)
        logger.info("first new-day record")
        logger.info("second new-day record")
    finally:
        handler.close()
        logger.handlers.clear()
        logger.propagate = True

    assert not tuple(tmp_path.glob("agentsystem.*.log"))
    assert active.read_text() == "first new-day record\nsecond new-day record\n"


def test_file_handler_cleans_only_matching_archives(tmp_path: Path) -> None:
    """保留期和数量清理不得触碰同目录的其他日志。"""
    active = tmp_path / "agentsystem.log"
    for name in (
        "agentsystem.2026-08-10.001.log",
        "agentsystem.2026-08-13.001.log",
        "agentsystem.2026-08-14.001.log",
    ):
        (tmp_path / name).write_text(name)
    unrelated = tmp_path / "other.2026-08-10.001.log"
    unrelated.write_text("keep")
    handler = DateSizeRotatingFileHandler(
        active,
        max_bytes=1_024,
        retention_days=2,
        max_backup_files=2,
        clock=lambda: datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    logger = logging.getLogger("tests.runtime.logging.cleanup")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        logger.info("current")
        handler.doRollover()
    finally:
        handler.close()
        logger.handlers.clear()
        logger.propagate = True

    assert not (tmp_path / "agentsystem.2026-08-10.001.log").exists()
    assert not (tmp_path / "agentsystem.2026-08-13.001.log").exists()
    assert (tmp_path / "agentsystem.2026-08-14.001.log").exists()
    assert (tmp_path / "agentsystem.2026-08-15.001.log").exists()
    assert unrelated.read_text() == "keep"


def test_logging_runtime_close_is_idempotent(tmp_path: Path) -> None:
    """重复关闭不能重复操作已释放 Handler 或抛出异常。"""
    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )

    runtime.close()
    runtime.close()


def test_unexpected_exception_log_contains_error_id_and_sanitized_traceback(
    tmp_path: Path,
) -> None:
    """统一异常日志必须可关联、可定位，且不输出原始异常正文。"""
    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    logger = logging.getLogger("tests.logging.exception")

    try:
        try:
            raise RuntimeError("secret provider response")
        except RuntimeError as error:
            error_id = log_unexpected_exception(
                logger,
                "provider_failed",
                error,
                operation="context.provider",
                error_id="fixed-error-id",
                provider_name="memory",
            )
    finally:
        runtime.close()

    content = (tmp_path / "logs" / "agentsystem.log").read_text()
    assert error_id == "fixed-error-id"
    assert "provider_failed error_id=fixed-error-id" in content
    assert "operation=context.provider" in content
    assert "provider_name=memory" in content
    assert "Traceback (most recent call last)" in content
    assert "RuntimeError: message redacted" in content


def test_run_failure_log_preserves_expected_failure_traceback(
    tmp_path: Path,
) -> None:
    """AgentLoop 终态日志必须保留预期失败的真实异常正文。"""
    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    logger = logging.getLogger("tests.logging.run_failure")

    try:
        try:
            raise RunFailure(
                operation="llm",
                code="provider_error",
                message="LLM provider 返回错误",
                exception_message="private provider detail",
            )
        except RunFailure as failure:
            common_logging.log_run_failure(
                logger,
                "agent_loop_failed",
                failure,
                error_id="fixed-error-id",
                operation="llm",
                code="provider_error",
            )
    finally:
        runtime.close()

    content = (tmp_path / "logs" / "agentsystem.log").read_text()
    assert content.count("Traceback (most recent call last)") == 1
    assert "error_id=fixed-error-id" in content
    assert "code=provider_error" in content
    assert "private provider detail" in content
