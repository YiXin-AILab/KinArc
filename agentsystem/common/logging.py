"""Runtime 服务的标准库日志配置、上下文和滚动文件实现。"""

import logging
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from logging.handlers import BaseRotatingHandler
from pathlib import Path
from types import MappingProxyType

from agentsystem.common.errors import RunFailure, new_error_id, sanitized_exc_info
from agentsystem.config.models import RuntimeLoggingConfig

_CONTEXT_FIELDS = (
    "app_id",
    "user_id",
    "agent_id",
    "session_id",
    "run_id",
    "runtime_trace_id",
)
_EMPTY_CONTEXT: Mapping[str, str] = MappingProxyType({})
_LOG_CONTEXT: ContextVar[Mapping[str, str]] = ContextVar(
    "agentsystem_log_context",
    default=_EMPTY_CONTEXT,
)
_LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s%(context_section)s | "
    "%(funcName)s:%(lineno)d - %(message)s"
)
_ACTIVE_RUNTIME: "LoggingRuntime | None" = None


def log_unexpected_exception(
    logger: logging.Logger,
    event: str,
    error: BaseException,
    *,
    operation: str,
    error_id: str | None = None,
    **fields: str,
) -> str:
    """以统一字段记录脱敏异常堆栈并返回关联 ID。

    本函数不去重；最终所有权边界负责确保只记录一次。

    Args:
        logger: 所属业务模块的 Logger。
        event: 稳定日志事件名称。
        error: 需记录的异常，异常正文将被替换。
        operation: 失败操作名称。
        error_id: 已有的关联标识；未提供时生成。
        **fields: 附加诊断字段，调用方须保证值可安全记录。

    Returns:
        本条日志使用的关联标识。
    """
    resolved_error_id = error_id or new_error_id()
    field_text = "".join(f" {name}={value}" for name, value in fields.items())
    logger.exception(
        "%s error_id=%s operation=%s exception_type=%s%s",
        event,
        resolved_error_id,
        operation,
        type(error).__name__,
        field_text,
        exc_info=sanitized_exc_info(error),
        stacklevel=2,
    )
    return resolved_error_id


def log_run_failure(
    logger: logging.Logger,
    event: str,
    failure: RunFailure,
    *,
    error_id: str,
    operation: str,
    code: str,
) -> None:
    """在 AgentLoop 最终边界记录完整的终态失败异常链。

    Args:
        logger: 所属业务模块的 Logger。
        event: 稳定日志事件名称。
        failure: 包含原始正文的内部失败异常链。
        error_id: 已确定的日志与 Trace 关联标识。
        operation: 失败操作名称。
        code: 稳定错误码。

    此入口不脱敏、不去重；调用方负责日志访问边界及单次记录。
    """
    root = failure.__cause__ or failure
    logger.error(
        "%s error_id=%s operation=%s code=%s exception_type=%s",
        event,
        error_id,
        operation,
        code,
        type(root).__name__,
        exc_info=(type(failure), failure, failure.__traceback__),
        stacklevel=2,
    )


@contextmanager
def bind_log_context(**fields: str) -> Iterator[None]:
    """在当前同步或异步上下文内绑定日志字段，退出时恢复先前值。

    Args:
        **fields: 预定义的非空关联字段，如 session_id、run_id。

    Yields:
        None；with 块内的日志可读取合并后的上下文。

    Raises:
        ValueError: 字段未知或值不是非空字符串。
    """
    unknown = set(fields).difference(_CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"未知日志上下文字段：{', '.join(sorted(unknown))}")
    if any(not isinstance(value, str) or not value for value in fields.values()):
        raise ValueError("日志上下文字段必须是非空字符串")
    token = _LOG_CONTEXT.set(MappingProxyType({**_LOG_CONTEXT.get(), **fields}))
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


class _ContextFilter(logging.Filter):
    """把当前 ContextVar 中实际存在的关联值补充到 LogRecord。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """补充当前上下文的显示文本，并允许该记录通过。"""
        context = _LOG_CONTEXT.get()
        field_text = " ".join(
            f"{_context_label(field)}={context[field]}"
            for field in _CONTEXT_FIELDS
            if field in context
        )
        record.context_section = f" | {field_text}" if field_text else ""
        return True


class _MaximumLevelFilter(logging.Filter):
    """只允许不高于指定级别的记录通过。"""

    def __init__(self, maximum: int) -> None:
        """保存允许通过的最高日志级别，用于分离控制台输出流。"""
        super().__init__()
        self._maximum = maximum

    def filter(self, record: logging.LogRecord) -> bool:
        """判断记录是否不高于该输出端的最高级别。"""
        return record.levelno <= self._maximum


class DateSizeRotatingFileHandler(BaseRotatingHandler):
    """按本地自然日或文件大小滚动 UTF-8 日志的单进程 Handler。

    拥有活动文件句柄，继承 logging 的关闭生命周期；归档同时按日期和
    文件数量清理，仅匹配本活动文件的归档命名，不提供跨进程协调。
    """

    def __init__(
        self,
        filename: str | Path,
        *,
        max_bytes: int,
        retention_days: int,
        max_backup_files: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存滚动边界并立即打开活动 UTF-8 日志文件。

        Args:
            filename: 活动日志路径；父目录须已存在。
            max_bytes: 文件大小阈值，单位为编码后的字节。
            retention_days: 归档日期保留天数。
            max_backup_files: 保留期内的归档数量上限。
            clock: 可注入当前时间函数，默认使用本地时区。

        Raises:
            ValueError: 任一滚动边界不是正数。
            OSError: 文件无法访问或打开。
        """
        if max_bytes <= 0 or retention_days <= 0 or max_backup_files <= 0:
            raise ValueError("日志滚动边界必须为正数")
        self._max_bytes = max_bytes
        self._retention_days = retention_days
        self._max_backup_files = max_backup_files
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._active_date = self._initial_active_date(Path(filename))
        super().__init__(str(filename), mode="a", encoding="utf-8", delay=False)

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        """在日期变化或下一条记录将突破大小时请求滚动。

        Args:
            record: 尚未写入的日志记录，用于计算编码后的大小。

        Returns:
            非空活动文件需要归档时为 True。空文件不会因单条超大记录滚动。
        """
        current_date = self._clock().date()
        if current_date != self._active_date:
            if self._has_content():
                return True
            self._active_date = current_date
        if self.stream is None:
            self.stream = self._open()
        rendered = f"{self.format(record)}{self.terminator}"
        encoded_size = len(rendered.encode(self.encoding or "utf-8"))
        self.stream.seek(0, os.SEEK_END)
        current_size = self.stream.tell()
        return current_size > 0 and current_size + encoded_size > self._max_bytes

    def doRollover(self) -> None:
        """归档非空活动文件，重新打开文件并清理过期或超量归档。

        Raises:
            OSError: 归档重命名、重新打开或删除文件失败。
        """
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        active = Path(self.baseFilename)
        if active.exists() and active.stat().st_size > 0:
            os.replace(active, self._next_archive_path(self._active_date))
        self._active_date = self._clock().date()
        if not self.delay:
            self.stream = self._open()
        self._cleanup_archives()

    def _initial_active_date(self, filename: Path) -> date:
        if filename.exists() and filename.stat().st_size > 0:
            modified = datetime.fromtimestamp(filename.stat().st_mtime).astimezone()
            return modified.date()
        return self._clock().date()

    def _has_content(self) -> bool:
        active = Path(self.baseFilename)
        return active.exists() and active.stat().st_size > 0

    def _next_archive_path(self, archive_date: date) -> Path:
        active = Path(self.baseFilename)
        matches = self._archives()
        sequence = max(
            (
                item_sequence
                for item_date, item_sequence, _ in matches
                if item_date == archive_date
            ),
            default=0,
        )
        return active.with_name(_archive_name(active, archive_date, sequence + 1))

    def _cleanup_archives(self) -> None:
        cutoff = self._clock().date() - timedelta(days=self._retention_days)
        retained: list[tuple[date, int, Path]] = []
        for archive_date, sequence, path in self._archives():
            if archive_date < cutoff:
                path.unlink()
            else:
                retained.append((archive_date, sequence, path))
        retained.sort(key=lambda item: (item[0], item[1]))
        for _, _, path in retained[: -self._max_backup_files]:
            path.unlink()

    def _archives(self) -> list[tuple[date, int, Path]]:
        active = Path(self.baseFilename)
        pattern = _archive_pattern(active)
        archives: list[tuple[date, int, Path]] = []
        for candidate in active.parent.iterdir():
            match = pattern.fullmatch(candidate.name)
            if match is None:
                continue
            try:
                archive_date = date.fromisoformat(match.group("date"))
            except ValueError:
                continue
            archives.append((archive_date, int(match.group("sequence")), candidate))
        return archives


class LoggingRuntime:
    """拥有一次 setup_logging 创建的 root Handler 生命周期。

    close 仅移除自有 Handler 并恢复之前的 root 级别，不还原第三方 Logger
    的配置。实例由 setup_logging 创建，宿主退出时应关闭。
    """

    def __init__(
        self,
        handlers: tuple[logging.Handler, ...],
        *,
        previous_root_level: int,
    ) -> None:
        """保存自有 Handler 及关闭时需要恢复的 root 日志级别。"""
        self._handlers = handlers
        self._previous_root_level = previous_root_level
        self._closed = False

    def close(self) -> None:
        """从 root 移除并关闭自有 Handler；重复调用无副作用。"""
        global _ACTIVE_RUNTIME
        if self._closed:
            return
        self._closed = True
        root = logging.getLogger()
        for handler in self._handlers:
            root.removeHandler(handler)
            handler.flush()
            handler.close()
        root.setLevel(self._previous_root_level)
        if _ACTIVE_RUNTIME is self:
            _ACTIVE_RUNTIME = None


def setup_logging(
    config: RuntimeLoggingConfig,
    *,
    project_root: str | Path,
) -> LoggingRuntime:
    """配置当前进程的控制台、文件及第三方日志。

    关闭此前由本模块创建的运行实例，并调整 Uvicorn、LiteLLM Logger
    为向 root 传播；文件输出会创建目录和打开活动文件。

    Args:
        config: 已校验的日志输出和归档参数。
        project_root: 必须存在的项目根目录，相对日志目录以此为基准。

    Returns:
        持有新 Handler 的生命周期对象，宿主负责关闭。

    Raises:
        OSError: 根目录、日志目录或文件无法访问。
        ValueError: 解析后的日志目录逃逸项目根目录。
    """
    global _ACTIVE_RUNTIME
    if _ACTIVE_RUNTIME is not None:
        _ACTIVE_RUNTIME.close()
    root_path = Path(project_root).resolve(strict=True)
    formatter = logging.Formatter(
        _LOG_FORMAT,
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    context_filter = _ContextFilter()
    level = logging.getLevelNamesMapping()[config.level]
    handlers: list[logging.Handler] = []
    if config.console_enabled:
        handlers.extend(_console_handlers(formatter, context_filter, level))
    if config.file_enabled:
        handlers.append(
            _file_handler(
                config,
                root_path=root_path,
                formatter=formatter,
                context_filter=context_filter,
                level=level,
            )
        )
    root = logging.getLogger()
    previous_root_level = root.level
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)
    _configure_third_party_loggers(level)
    runtime = LoggingRuntime(tuple(handlers), previous_root_level=previous_root_level)
    _ACTIVE_RUNTIME = runtime
    return runtime


def _console_handlers(
    formatter: logging.Formatter,
    context_filter: logging.Filter,
    level: int,
) -> tuple[logging.Handler, logging.Handler]:
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(level)
    stdout.setFormatter(formatter)
    stdout.addFilter(context_filter)
    stdout.addFilter(_MaximumLevelFilter(logging.INFO))
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(max(level, logging.WARNING))
    stderr.setFormatter(formatter)
    stderr.addFilter(context_filter)
    return stdout, stderr


def _file_handler(
    config: RuntimeLoggingConfig,
    *,
    root_path: Path,
    formatter: logging.Formatter,
    context_filter: logging.Filter,
    level: int,
) -> logging.Handler:
    directory = (root_path / config.directory).resolve()
    try:
        directory.relative_to(root_path)
    except ValueError as error:
        raise ValueError("日志目录必须位于项目根目录内") from error
    directory.mkdir(parents=True, exist_ok=True)
    handler = DateSizeRotatingFileHandler(
        directory / config.filename,
        max_bytes=config.max_bytes,
        retention_days=config.retention_days,
        max_backup_files=config.max_backup_files,
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(context_filter)
    return handler


def _configure_third_party_loggers(level: int) -> None:
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "uvicorn.asgi",
        "LiteLLM",
    ):
        logger = logging.getLogger(name)
        for handler in tuple(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(level)
        logger.propagate = True


def _context_label(field: str) -> str:
    return "segment" if field == "runtime_trace_id" else field.removesuffix("_id")


def _archive_name(active: Path, archive_date: date, sequence: int) -> str:
    return f"{active.stem}.{archive_date.isoformat()}.{sequence:03d}{active.suffix}"


def _archive_pattern(active: Path) -> re.Pattern[str]:
    return re.compile(
        rf"{re.escape(active.stem)}\."
        rf"(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\."
        rf"(?P<sequence>\d{{3,}}){re.escape(active.suffix)}"
    )
