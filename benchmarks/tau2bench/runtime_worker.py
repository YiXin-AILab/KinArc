"""在主项目环境中执行原生 Tau2 预检和精确 Redis 清理。"""

import asyncio
import logging
import re
import sys
from pathlib import Path

from agentsystem.common.logging import log_unexpected_exception
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import LocalAssetStoreConfig, RuntimeConfig
from agentsystem.redis.client import close_redis_client, create_redis_client

LOGGER = logging.getLogger(__name__)


async def check_or_clean(root: Path, mode: str) -> None:
    """校验运行资源，或仅删除当前尝试拥有的 Redis 前缀。"""
    runtime = load_yaml_config(root / "runtime.yaml", RuntimeConfig)
    if (
        runtime.managers.message.type != "redis"
        or runtime.managers.state.type != "redis"
    ):
        raise ValueError("native benchmark requires Redis Message/State managers")
    if not isinstance(runtime.asset_store, LocalAssetStoreConfig):
        raise ValueError("native benchmark requires local assets")
    if mode == "check":
        from server.entrypoint import load_service_assets

        load_service_assets(
            runtime_config_path="runtime.yaml",
            applications_root="application",
            project_root=root,
        )
    elif mode != "clean":
        raise ValueError("unknown Runtime helper operation")
    if re.fullmatch(r"tau2native:[0-9a-f]{32}", runtime.redis.key_prefix) is None:
        raise ValueError("refusing non-owned Redis prefix")
    client = await create_redis_client(runtime.redis)
    try:
        if mode == "clean":
            async for key in client.scan_iter(match=f"{runtime.redis.key_prefix}:*"):
                await client.delete(key)
    finally:
        await close_redis_client(client)


def main() -> None:
    """返回进程状态，不输出配置值。"""
    try:
        asyncio.run(check_or_clean(Path(sys.argv[2]), sys.argv[1]))
    except Exception as error:
        log_unexpected_exception(
            LOGGER, "native_tau2_helper_failed", error, operation="tau2.runtime.helper"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
