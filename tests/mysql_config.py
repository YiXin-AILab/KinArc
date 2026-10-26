"""真实 MySQL 合约测试的安全本地配置。"""

import os
from collections.abc import Mapping

import pytest
from agentsystem.config.models import MysqlDatabaseConfig


def local_mysql_config(
    environ: Mapping[str, str] = os.environ,
) -> MysqlDatabaseConfig:
    """从 Runtime MySQL 环境变量读取项目唯一的本地数据库配置。"""
    return MysqlDatabaseConfig(
        host=environ.get("RUNTIME_MYSQL_HOST", "127.0.0.1"),
        port=int(environ.get("RUNTIME_MYSQL_HOST_PORT", "3306")),
        name=environ.get("RUNTIME_MYSQL_DATABASE", "agentsystem"),
        username=environ.get("RUNTIME_MYSQL_USER", "agentsystem"),
        password=environ.get("RUNTIME_MYSQL_PASSWORD"),
        connect_timeout_seconds=5,
        pool_size=10,
        max_overflow=10,
        pool_timeout_seconds=5,
        operation_timeout_seconds=5,
    )


def require_local_mysql_config(
    environ: Mapping[str, str] = os.environ,
) -> MysqlDatabaseConfig:
    """缺少显式 Runtime MySQL 凭证时跳过真实外部测试。"""
    password = environ.get("RUNTIME_MYSQL_PASSWORD")
    if password is None or not password.strip():
        pytest.skip("真实 MySQL 测试需设置非空 RUNTIME_MYSQL_PASSWORD")
    return local_mysql_config(environ)
