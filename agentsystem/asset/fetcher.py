"""带 SSRF、重定向、timeout 和大小门禁的媒体 URL 获取器。"""

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from agentsystem.common.errors import AdapterError

type HostLookup = Callable[[str, int], Awaitable[tuple[str, ...]]]


class SafeAddressResolver(AbstractResolver):
    """只把已经验证为公网的 DNS 结果交给 aiohttp 连接器。

    解析与连接共用这组地址，避免连接时二次 DNS 查询绕过检查。实例不持有独立资源。
    """

    def __init__(self, *, lookup: HostLookup | None = None) -> None:
        """绑定 DNS 查询入口。

        Args:
            lookup: 可注入的查询函数；缺省使用事件循环的地址解析。
        """
        self._lookup = lookup or _default_lookup

    async def resolve_host(self, host: str, port: int) -> tuple[str, ...]:
        """解析一次 hostname，并整体拒绝任何非公网结果。

        Args:
            host: 主机名或 IP 字面量。
            port: DNS 查询使用的目标端口。

        Returns:
            去重且保持解析顺序的公网地址。

        Raises:
            AdapterError: 解析结果为空或包含非公网地址。
        """
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            addresses = await self._lookup(host, port)
        else:
            addresses = (str(literal),)
        if not addresses or any(not _is_public(address) for address in addresses):
            raise _media_error("invalid_media_source", "媒体 URL 目标不是公网地址")
        return tuple(dict.fromkeys(addresses))

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        """返回实际连接使用的已验证地址，避免二次系统 DNS 查询。

        Args:
            host: 目标主机名或 IP 字面量。
            port: 连接端口。
            family: aiohttp 请求的地址族；支持 IPv4、IPv6 和未指定地址族。

        Returns:
            带实际 IP 地址族和数字地址标志的连接记录。

        Raises:
            AdapterError: 解析结果为空或存在非公网地址。
        """
        addresses = await self.resolve_host(host, port)
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=(
                    socket.AF_INET6
                    if ipaddress.ip_address(address).version == 6
                    else socket.AF_INET
                ),
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_NUMERICHOST,
            )
            for address in addresses
            if family in {socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6}
        ]

    async def close(self) -> None:
        """默认 resolver 不持有独立资源。"""


class SafeMediaFetcher:
    """通过共享异步 Session 有界获取 HTTP/HTTPS 媒体。

    不管理 Session 生命周期；调用方须给连接器配置 SafeAddressResolver，
    才能对域名的实际连接地址执行公网门禁。本类负责逐跳 URL、大小和总时限检查。
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        max_file_bytes: int,
        timeout_seconds: float,
        chunk_size: int = 64 * 1024,
        max_redirects: int = 3,
        allowed_url_schemes: tuple[str, ...] = ("http", "https"),
    ) -> None:
        """保存共享 Session，并校验下载限制。

        Args:
            session: 配置安全地址解析器的异步 HTTP Session。
            max_file_bytes: 单文件最大字节数。
            timeout_seconds: 包含重定向和正文消费的总超时秒数。
            chunk_size: 每次产生的最大字节块大小。
            max_redirects: 允许的重定向次数，可为零。
            allowed_url_schemes: 允许的 HTTP/HTTPS scheme 子集。

        Raises:
            ValueError: 大小、超时、分块、重定向或 scheme 配置无效。
        """
        if max_file_bytes <= 0 or timeout_seconds <= 0 or chunk_size <= 0:
            raise ValueError("Fetcher 大小、timeout 和 chunk_size 必须为正数")
        if max_redirects < 0:
            raise ValueError("max_redirects 不能为负数")
        normalized_schemes = tuple(scheme.lower() for scheme in allowed_url_schemes)
        if not normalized_schemes or any(
            scheme not in {"http", "https"} for scheme in normalized_schemes
        ):
            raise ValueError("Fetcher URL scheme 配置无效")
        self._session = session
        self._max_file_bytes = max_file_bytes
        self._timeout_seconds = timeout_seconds
        self._chunk_size = chunk_size
        self._max_redirects = max_redirects
        self._allowed_url_schemes = frozenset(normalized_schemes)

    async def fetch(self, url: str) -> AsyncIterator[bytes]:
        """逐跳校验并流式输出媒体，归一化预期网络和超时错误。

        Args:
            url: 初始媒体地址。

        Yields:
            非空正文块；消费完毕或显式关闭生成器时释放当前响应。

        Raises:
            AdapterError: 地址、重定向、状态码、大小、网络或超时检查失败。
        """
        current_url = validate_media_url(url, self._allowed_url_schemes)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                for redirect_count in range(self._max_redirects + 1):
                    async with self._session.get(
                        current_url,
                        allow_redirects=False,
                    ) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if (
                                location is None
                                or redirect_count == self._max_redirects
                            ):
                                raise _media_error(
                                    "media_download_failed",
                                    "媒体重定向无效",
                                    retryable=True,
                                )
                            current_url = validate_media_url(
                                urljoin(current_url, location),
                                self._allowed_url_schemes,
                            )
                            continue
                        if not 200 <= response.status < 300:
                            raise _media_error(
                                "media_download_failed",
                                "媒体下载失败",
                                retryable=True,
                            )
                        _validate_content_length(
                            response.headers.get("Content-Length"),
                            self._max_file_bytes,
                        )
                        size_bytes = 0
                        async for chunk in response.content.iter_chunked(
                            self._chunk_size
                        ):
                            size_bytes += len(chunk)
                            if size_bytes > self._max_file_bytes:
                                raise _media_error(
                                    "media_too_large",
                                    "媒体超过单文件上限",
                                )
                            if chunk:
                                yield chunk
                        return
        except AdapterError:
            raise
        except (TimeoutError, aiohttp.ClientError) as error:
            raise _media_error(
                "media_download_failed",
                "媒体下载失败",
                retryable=True,
            ) from error


def validate_media_url(
    url: str,
    allowed_url_schemes: frozenset[str] | None = None,
) -> str:
    """校验 URL 结构，并在字面 IP 场景立即执行 SSRF 门禁。

    域名的 DNS 安全检查由连接器上的 SafeAddressResolver 完成。

    Args:
        url: 待校验地址。
        allowed_url_schemes: 允许的 scheme；未提供或为空时使用 HTTP/HTTPS。

    Returns:
        通过检查的原始 URL。

    Raises:
        AdapterError: URL 结构或目标 IP 不符合媒体获取限制。
    """
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise _media_error("invalid_media_source", "媒体 URL 包含控制字符")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise _media_error("invalid_media_source", "媒体 URL 结构无效") from error
    allowed = allowed_url_schemes or frozenset({"http", "https"})
    if (
        parsed.scheme.lower() not in allowed
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise _media_error("invalid_media_source", "媒体 URL 结构无效")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return url
    if not address.is_global:
        raise _media_error("invalid_media_source", "媒体 URL 目标不是公网地址")
    return url


async def _default_lookup(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        results = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise _media_error(
            "media_download_failed",
            "媒体 DNS 解析失败",
            retryable=True,
        ) from error
    return tuple(str(item[4][0]) for item in results)


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _validate_content_length(value: str | None, max_file_bytes: int) -> None:
    if value is None:
        return
    try:
        content_length = int(value)
    except ValueError as error:
        raise _media_error(
            "media_download_failed",
            "媒体 Content-Length 无效",
            retryable=True,
        ) from error
    if content_length > max_file_bytes:
        raise _media_error("media_too_large", "媒体超过单文件上限")


def _media_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> AdapterError:
    """构造不会泄露 URL 或响应正文的媒体 adapter 错误。"""
    return AdapterError(
        operation="media.fetch",
        code=code,
        message=message,
        retryable=retryable,
    )
