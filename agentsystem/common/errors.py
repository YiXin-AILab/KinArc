"""全项目统一的行为异常与结构化错误。"""

import re
import traceback
from enum import StrEnum
from types import TracebackType
from typing import Literal
from uuid import uuid4

from pydantic import Field

from agentsystem.common.models import StrictModel

_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

type ErrorOperation = Literal["run", "llm", "tool", "compaction"]


def validate_error_code(code: str) -> str:
    """校验调用点自定义的稳定 snake_case 错误码。

    Args:
        code: 小写字母开头、仅含小写字母、数字和下划线的错误码。

    Returns:
        校验通过的原始错误码。

    Raises:
        ValueError: 格式不符或长度超过 64。
    """
    if _ERROR_CODE_PATTERN.fullmatch(code) is None:
        raise ValueError("error code 必须是最长 64 字符的 snake_case")
    return code


def new_error_id() -> str:
    """生成只用于关联内部日志与 Trace 的不透明标识。

    Returns:
        新 UUID 的十六进制文本，不包含业务身份。
    """
    return uuid4().hex


def sanitized_exc_info(
    error: BaseException,
) -> tuple[type[BaseException], BaseException, TracebackType | None]:
    """生成保留栈帧及异常链、替换异常正文的日志参数。

    Args:
        error: 原始异常，不会修改该对象。

    Returns:
        logging exc_info 所需的类型、脱敏异常和原始 traceback。
        异常消息替换为类型提示，循环引用在重复节点处截断。
    """
    sanitized = _sanitized_exception(error, seen=set())
    return type(sanitized), sanitized, error.__traceback__


def _sanitized_exception(
    error: BaseException,
    *,
    seen: set[int],
) -> RuntimeError:
    """复制异常链并遮蔽正文；用对象身份集合截断循环 cause/context。"""
    sanitized = RuntimeError(f"{type(error).__name__}: message redacted")
    if id(error) in seen:
        return sanitized
    seen.add(id(error))
    cause = error.__cause__
    if cause is not None:
        sanitized.__cause__ = _sanitized_exception(cause, seen=seen).with_traceback(
            cause.__traceback__
        )
    elif error.__context__ is not None and not error.__suppress_context__:
        context = error.__context__
        sanitized.__context__ = _sanitized_exception(context, seen=seen).with_traceback(
            context.__traceback__
        )
    return sanitized


class ServiceErrorKind(StrEnum):
    """决定同步请求传输行为的封闭错误类别。"""

    INVALID = "invalid"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    CAPACITY = "capacity"
    UNAVAILABLE = "unavailable"


class ToolError(Exception):
    """Tool 的预期失败，由 ToolExecutor 转换为模型可见的错误结果。

    Attributes:
        code: 稳定错误码。
        message: 调用方提供的安全说明；本类不自动脱敏。
    """

    def __init__(
        self,
        *,
        code: str = "tool_error",
        message: str = "工具执行失败",
    ) -> None:
        """使用模型可见的安全信息构造预期 Tool 失败。

        Args:
            code: 稳定错误码。
            message: 可向模型展示的错误说明。

        Raises:
            ValueError: code 不符合错误码格式。
        """
        super().__init__(message)
        self.code = validate_error_code(code)
        self.message = message


class AdapterError(Exception):
    """隔离第三方 SDK 异常类型的依赖边界失败。

    Attributes:
        code: 稳定错误码。
        message: 调用方提供的安全说明，不应包含第三方响应正文。
        operation: 失败操作的内部名称。
        retryable: 是否具有重试意义；本类不执行重试。
    """

    def __init__(
        self,
        *,
        operation: str,
        code: str = "adapter_error",
        message: str = "依赖服务调用失败",
        retryable: bool = False,
    ) -> None:
        """构造依赖失败；调用方须预先处理消息中的敏感内容。

        Args:
            operation: 失败操作的内部名称。
            code: 稳定错误码。
            message: 已由调用方处理的安全说明。
            retryable: 标记错误是否可重试。

        Raises:
            ValueError: code 不符合错误码格式。
        """
        super().__init__(message)
        self.code = validate_error_code(code)
        self.message = message
        self.operation = operation
        self.retryable = retryable


class ServiceError(Exception):
    """同步请求边界可稳定映射到传输层的错误。

    Attributes:
        kind: 用于选择传输状态的封闭分类。
        code: 面向调用方的稳定错误码。
        message: 可公开的说明，由构造方保证不含敏感信息。
    """

    def __init__(
        self,
        *,
        kind: ServiceErrorKind,
        code: str = "request_failed",
        message: str = "请求处理失败",
    ) -> None:
        """用传输类别、自定义 code 和安全消息构造请求错误。

        Args:
            kind: 传输层错误分类。
            code: 稳定错误码。
            message: 可公开的请求失败说明。

        Raises:
            ValueError: code 不符合错误码格式。
        """
        super().__init__(message)
        self.kind = kind
        self.code = validate_error_code(code)
        self.message = message


class RunFailure(Exception):
    """向 AgentLoop 终态边界传递可收敛为 FAILED 的执行失败。

    Attributes:
        code: 稳定错误码。
        message: 用于结构化结果的安全说明。
        operation: 失败所属的 run、LLM、Tool 或压缩操作。
        retryable: 是否具有重试意义，不触发自动重放。
        notify_middleware: 终态边界是否应通知错误中间件。
    """

    def __init__(
        self,
        *,
        operation: ErrorOperation,
        code: str = "execution_failed",
        message: str = "执行失败",
        exception_message: str | None = None,
        retryable: bool = False,
        notify_middleware: bool = True,
    ) -> None:
        """构造仅由 AgentLoop 终态边界消费的失败信号。

        Args:
            operation: 失败操作类别。
            code: 稳定错误码。
            message: 用于结构化结果的安全说明。
            exception_message: 内部异常正文；为空时使用 message。
            retryable: 是否标记为可重试。
            notify_middleware: 是否允许终态边界通知错误中间件。

        Raises:
            ValueError: code 不符合错误码格式。
        """
        super().__init__(exception_message or message)
        self.code = validate_error_code(code)
        self.message = message
        self.operation = operation
        self.retryable = retryable
        self.notify_middleware = notify_middleware


class ResponseFormatError(Exception):
    """对端响应不符合公开协议结构的预期格式错误。"""

    def __init__(
        self,
        *,
        code: str = "invalid_response_format",
        message: str = "响应格式不符合预期",
    ) -> None:
        """用稳定 code 和不包含响应正文的消息构造格式错误。

        Args:
            code: 稳定错误码。
            message: 已由调用方处理敏感信息的说明。
        """
        super().__init__(message)
        self.code = validate_error_code(code)
        self.message = message


class EventCommitError(RuntimeError):
    """事件投影失败后向宿主传播的 fail-close 信号。

    已成功的前序投影不回滚；消费方必须停止后续投影和 Loop。

    Attributes:
        code: 固定的提交失败错误码。
        operation: 失败操作名称。
        cause: 原始异常引用；仅保存，不自动设置异常链。
    """

    def __init__(
        self,
        cause: Exception | None = None,
        *,
        operation: str = "event.commit",
    ) -> None:
        """标记不能再安全产生终态 Event 的提交失败。

        Args:
            cause: 触发提交失败的原始异常，可省略。
            operation: 用于诊断的内部操作名称。
        """
        super().__init__("事件提交失败")
        self.code = "event_commit_failed"
        self.operation = operation
        self.cause = cause


class LLMError(StrictModel):
    """LLM adapter 输出的结构化错误，不暴露供应商异常类型。

    Attributes:
        code: 稳定错误码。
        message: adapter 提供的安全说明。
        retryable: 是否具有重试意义。
        provider: 可选供应商名称。
    """

    code: str = Field(
        default="llm_error",
        min_length=1,
        max_length=64,
        pattern=_ERROR_CODE_PATTERN,
    )
    message: str = Field(default="模型调用失败", min_length=1)
    retryable: bool = False
    provider: str | None = None


class RunError(StrictModel):
    """保存 run 终态错误及内部诊断信息的值模型。

    公开投影必须自行选择安全字段，不能直接公开完整模型。

    Attributes:
        code: 稳定错误码。
        message: 可公开的安全说明。
        retryable: 是否具有重试意义。
        error_id: 日志与 Trace 的关联标识。
        exception_type: 内部异常类型名。
        stacktrace: 内部完整异常链，可能含敏感正文。
    """

    code: str = Field(
        default="execution_failed",
        min_length=1,
        max_length=64,
        pattern=_ERROR_CODE_PATTERN,
    )
    message: str = Field(default="执行失败", min_length=1)
    retryable: bool = False
    error_id: str | None = Field(default=None, min_length=1)
    exception_type: str | None = Field(default=None, min_length=1)
    stacktrace: str | None = Field(
        default=None,
        min_length=1,
    )


def format_run_failure(failure: RunFailure) -> tuple[str, str]:
    """格式化根异常类型和完整异常链，不执行正文脱敏或长度截断。

    Args:
        failure: 最终失败信号及其 cause 链。

    Returns:
        根异常类型名和 traceback 文本；查找根因时防止循环引用。
    """
    root: BaseException = failure
    seen: set[int] = set()
    while root.__cause__ is not None and id(root) not in seen:
        seen.add(id(root))
        root = root.__cause__
    exception_type = _exception_type_name(root)
    stacktrace = "".join(traceback.format_exception(failure))
    return exception_type, stacktrace


def _exception_type_name(error: BaseException) -> str:
    error_type = type(error)
    if error_type.__module__ == "builtins":
        return error_type.__qualname__
    return f"{error_type.__module__}.{error_type.__qualname__}"


__all__ = [
    "AdapterError",
    "ErrorOperation",
    "EventCommitError",
    "LLMError",
    "ResponseFormatError",
    "RunError",
    "RunFailure",
    "ServiceError",
    "ServiceErrorKind",
    "ToolError",
    "format_run_failure",
    "new_error_id",
    "sanitized_exc_info",
    "validate_error_code",
]
