"""Langfuse live tests 共用的 observation 查询客户端。"""

import asyncio
import base64
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def parse_langfuse_otel_connection(
    endpoint: str,
    authorization: str,
) -> tuple[str, str, str]:
    """从标准 OTel 配置提取 Langfuse API 连接信息。"""
    trace_path = "/api/public/otel/v1/traces"
    if not endpoint.endswith(trace_path):
        raise ValueError("OTEL_ENDPOINT 不是 Langfuse OTLP traces endpoint")
    scheme, encoded = authorization.split(maxsplit=1)
    if scheme.lower() != "basic":
        raise ValueError("OTEL_AUTHORIZATION 必须使用 Basic scheme")
    credentials = base64.b64decode(encoded, validate=True).decode("utf-8")
    public_key, separator, secret_key = credentials.partition(":")
    if not separator or not public_key or not secret_key:
        raise ValueError("OTEL_AUTHORIZATION 缺少 Langfuse public/secret key")
    return endpoint.removesuffix(trace_path).rstrip("/"), public_key, secret_key


async def wait_for_langfuse_observations(
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    marker: str,
    required_types: set[str],
    timeout_seconds: float = 10.0,
) -> list[dict[str, object]]:
    """按唯一输入标记等待 Langfuse 返回完整 observation 集合。"""
    if timeout_seconds <= 0:
        raise ValueError("Langfuse 查询 timeout 必须为正数")
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                roots = await asyncio.to_thread(
                    _fetch_observations,
                    base_url,
                    public_key,
                    secret_key,
                    "name",
                    "invoke_agent",
                )
                trace_id = _trace_id_for_marker(roots, marker)
                if trace_id is not None:
                    document = await asyncio.to_thread(
                        _fetch_observations,
                        base_url,
                        public_key,
                        secret_key,
                        "traceId",
                        trace_id,
                    )
                    observations = _observations(document)
                    if {item.get("type") for item in observations} >= required_types:
                        return observations
                await asyncio.sleep(0.5)
    except TimeoutError as error:
        raise AssertionError("Langfuse 未返回本次测试的完整 trace") from error


def _trace_id_for_marker(document: object, marker: str) -> str | None:
    """从根 observations 中定位包含本次唯一输入的 traceId。"""
    for item in _observations(document):
        if marker not in json.dumps(item.get("input"), ensure_ascii=False):
            continue
        trace_id = item.get("traceId")
        if isinstance(trace_id, str):
            return trace_id
    return None


def _observations(document: object) -> list[dict[str, object]]:
    """把 Langfuse 文档收敛为类型明确的 observation 列表。"""
    if not isinstance(document, dict):
        return []
    data = document.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _fetch_observations(
    base_url: str,
    public_key: str,
    secret_key: str,
    column: str,
    value: str,
) -> object:
    """通过 Langfuse v2 公共 API 获取一组 observations。"""
    credentials = f"{public_key}:{secret_key}".encode()
    observation_filter = json.dumps(
        [
            {
                "type": "string",
                "column": column,
                "operator": "=",
                "value": value,
            }
        ]
    )
    query = urlencode(
        {
            "filter": observation_filter,
            "fields": "core,basic,io,usage",
            "limit": 100,
        }
    )
    request = Request(
        f"{base_url.rstrip('/')}/api/public/v2/observations?{query}",
        headers={
            "Authorization": f"Basic {base64.b64encode(credentials).decode('ascii')}"
        },
    )
    with urlopen(request, timeout=5) as response:  # noqa: S310 - 测试目标由环境指定
        return json.load(response)
