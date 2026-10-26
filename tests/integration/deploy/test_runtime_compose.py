"""Runtime 本地基础设施 Compose 的真实渲染与持久化合约。"""

import json
import os
import subprocess
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from redis.asyncio import Redis

type S3Settings = tuple[str, str, str, str, str]

_COMPOSE_PATH = Path("deploy/runtime/docker-compose.yml")
_REDIS_CONFIG_PATH = Path("deploy/runtime/redis.conf")
_OPT_IN = "TEST_RUNTIME_INFRA_PERSISTENCE"
_REDIS_TEST_PORT = os.environ.get("TEST_RUNTIME_REDIS_PORT", "6399")
_MINIO_TEST_PORT = os.environ.get("TEST_RUNTIME_MINIO_PORT", "9192")
_MINIO_TEST_CONSOLE_PORT = os.environ.get(
    "TEST_RUNTIME_MINIO_CONSOLE_PORT",
    "9193",
)
_MINIO_TEST_ACCESS_KEY = os.environ.get(
    "TEST_ASSET_S3_ACCESS_KEY",
    f"test-{uuid4().hex}",
)
_MINIO_TEST_SECRET_KEY = os.environ.get(
    "TEST_ASSET_S3_SECRET_KEY",
    f"test-{uuid4().hex}{uuid4().hex}",
)
_MINIO_TEST_BUCKET = os.environ.get(
    "TEST_ASSET_S3_BUCKET",
    "agentsystem-assets",
)
_MYSQL_TEST_PORT = os.environ.get("TEST_RUNTIME_MYSQL_PORT", "3399")
_MYSQL_TEST_DATABASE = f"runtime_{uuid4().hex}"
_MYSQL_TEST_USER = f"user_{uuid4().hex[:16]}"
_MYSQL_TEST_PASSWORD = f"password-{uuid4().hex}"
_MYSQL_TEST_ROOT_PASSWORD = f"root-{uuid4().hex}"


def _compose_environment() -> dict[str, str]:
    """为真实重建测试使用不占用默认端口的显式环境。"""
    environment = dict(os.environ)
    for name in ("REDIS_HOST", "DATABASE_HOST", "TEST_ASSET_S3_ENDPOINT"):
        environment.pop(name, None)
    environment["RUNTIME_REDIS_HOST_PORT"] = _REDIS_TEST_PORT
    environment["TEST_ASSET_S3_ACCESS_KEY"] = _MINIO_TEST_ACCESS_KEY
    environment["TEST_ASSET_S3_SECRET_KEY"] = _MINIO_TEST_SECRET_KEY
    environment["TEST_ASSET_S3_BUCKET"] = _MINIO_TEST_BUCKET
    environment["TEST_ASSET_S3_PORT"] = _MINIO_TEST_PORT
    environment["TEST_ASSET_S3_CONSOLE_PORT"] = _MINIO_TEST_CONSOLE_PORT
    environment["RUNTIME_MYSQL_HOST_PORT"] = _MYSQL_TEST_PORT
    environment["DATABASE_NAME"] = _MYSQL_TEST_DATABASE
    environment["DATABASE_USERNAME"] = _MYSQL_TEST_USER
    environment["DATABASE_PASSWORD"] = _MYSQL_TEST_PASSWORD
    environment["RUNTIME_MYSQL_ROOT_PASSWORD"] = _MYSQL_TEST_ROOT_PASSWORD
    return environment


def _compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """在有限 timeout 内执行项目专用 Compose 命令。"""
    return subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_PATH), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=_compose_environment(),
    )


def _render_compose() -> dict[str, Any]:
    """返回 Docker Compose 实际归一化后的项目模型。"""
    rendered = _compose("config", "--format", "json")
    return cast(dict[str, Any], json.loads(rendered.stdout))


def _render_redis_compose() -> dict[str, Any]:
    """返回启用 Redis profile 后的归一化 Compose 模型。"""
    rendered = _compose("--profile", "redis", "config", "--format", "json")
    return cast(dict[str, Any], json.loads(rendered.stdout))


def _render_sql_compose() -> dict[str, Any]:
    """返回启用 SQL profile 后的归一化 Compose 模型。"""
    rendered = _compose("--profile", "sql", "config", "--format", "json")
    return cast(dict[str, Any], json.loads(rendered.stdout))


def _render_s3_compose() -> dict[str, Any]:
    """返回启用 S3 profile 后的归一化 Compose 模型。"""
    rendered = _compose("--profile", "s3", "config", "--format", "json")
    return cast(dict[str, Any], json.loads(rendered.stdout))


def test_redis_service_renders_without_asset_credentials() -> None:
    """只启动 Redis 时不得强制配置未使用的 MinIO 凭证。"""
    environment = dict(os.environ)
    environment.pop("TEST_ASSET_S3_ACCESS_KEY", None)
    environment.pop("TEST_ASSET_S3_SECRET_KEY", None)

    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(_COMPOSE_PATH),
            "--profile",
            "redis",
            "config",
            "--format",
            "json",
            "redis",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    compose = cast(dict[str, Any], json.loads(rendered.stdout))

    assert set(compose["services"]) == {"redis"}


def test_default_compose_has_no_enabled_services() -> None:
    """所有中间件均按需启用，默认 Compose 不启动任何服务。"""
    compose = _render_compose()

    assert not compose["services"]


def test_all_profiles_exclude_agent_service() -> None:
    """启用全部中间件也不应包含 Agent 服务或其资产卷。"""
    rendered = _compose("--profile", "*", "config", "--format", "json")
    compose = json.loads(rendered.stdout)

    assert set(compose["services"]) == {
        "redis",
        "mysql",
        "mysql-migrate",
        "minio",
        "minio-init",
    }
    assert "kinarc_assets" not in compose.get("volumes", {})


def _require_persistence_opt_in() -> None:
    """仅在显式允许重建项目专用容器时继续。"""
    if os.environ.get(_OPT_IN) != "1":
        pytest.skip(f"需要 {_OPT_IN}=1")


def _redis_url() -> str:
    """返回持久化验证使用的隔离 Redis DB。"""
    return f"redis://127.0.0.1:{_REDIS_TEST_PORT}/15"


def _s3_settings() -> S3Settings:
    """返回真实重建测试使用的隔离 MinIO 配置。"""
    return (
        f"http://127.0.0.1:{_MINIO_TEST_PORT}",
        _MINIO_TEST_BUCKET,
        "us-east-1",
        _MINIO_TEST_ACCESS_KEY,
        _MINIO_TEST_SECRET_KEY,
    )


def _s3_client(settings: S3Settings) -> AbstractAsyncContextManager[Any]:
    """创建具有有限连接和读取 timeout 的测试 client。"""
    endpoint, _, region, access_key, secret_key = settings
    return get_session().create_client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=AioConfig(connect_timeout=2, read_timeout=5),
    )


def test_rendered_runtime_redis_is_persistent_and_loopback_only() -> None:
    """错误端口、配置挂载或数据卷会破坏本地恢复边界。"""
    compose = _render_redis_compose()
    redis = compose["services"]["redis"]
    mounts = {mount["target"]: mount for mount in redis["volumes"]}

    assert compose["name"] == "kinarc"
    assert redis["image"] == "redis:8-alpine"
    assert redis["command"] == [
        "redis-server",
        "/usr/local/etc/redis/redis.conf",
    ]
    assert redis["ports"] == [
        {
            "mode": "ingress",
            "target": 6379,
            "published": "6399",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert mounts["/data"]["type"] == "volume"
    assert mounts["/data"]["source"] == "kinarc_redis_data"
    assert mounts["/usr/local/etc/redis/redis.conf"]["type"] == "bind"
    assert mounts["/usr/local/etc/redis/redis.conf"]["read_only"] is True
    assert Path(mounts["/usr/local/etc/redis/redis.conf"]["source"]) == (
        _REDIS_CONFIG_PATH.resolve()
    )
    assert compose["volumes"]["kinarc_redis_data"]["name"] == "kinarc_redis_data"


def test_rendered_runtime_minio_is_persistent_and_loopback_only() -> None:
    """tmpfs、错误端口或共享数据卷会破坏 Runtime MinIO 边界。"""
    compose = _render_s3_compose()
    minio = compose["services"]["minio"]
    initializer = compose["services"]["minio-init"]
    mounts = {mount["target"]: mount for mount in minio["volumes"]}

    assert minio["ports"] == [
        {
            "mode": "ingress",
            "target": 9000,
            "published": "9192",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        },
        {
            "mode": "ingress",
            "target": 9001,
            "published": "9193",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        },
    ]
    assert mounts["/data"]["type"] == "volume"
    assert mounts["/data"]["source"] == "kinarc_minio_data"
    assert "tmpfs" not in minio
    assert initializer["depends_on"]["minio"]["condition"] == "service_healthy"
    assert initializer["restart"] == "no"
    assert compose["volumes"]["kinarc_minio_data"]["name"] == "kinarc_minio_data"


def test_rendered_runtime_mysql_is_profiled_persistent_and_healthy() -> None:
    """开发 MySQL 必须使用独立 named volume 并等待健康后迁移。"""
    default_compose = _render_compose()
    compose = _render_sql_compose()
    mysql = compose["services"]["mysql"]
    migration = compose["services"]["mysql-migrate"]
    mounts = {mount["target"]: mount for mount in mysql["volumes"]}

    assert "mysql" not in default_compose["services"]
    assert "mysql-migrate" not in default_compose["services"]
    assert "minio" not in compose["services"]
    assert mysql["image"] == "mysql:8.4"
    assert mysql["ports"] == [
        {
            "mode": "ingress",
            "target": 3306,
            "published": "3399",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert mounts["/var/lib/mysql"]["type"] == "volume"
    assert mounts["/var/lib/mysql"]["source"] == "kinarc_mysql_data"
    assert "tmpfs" not in mysql
    assert mysql["healthcheck"]["test"]
    assert migration["depends_on"]["mysql"]["condition"] == "service_healthy"
    assert migration["restart"] == "no"
    assert compose["volumes"]["kinarc_mysql_data"]["name"] == "kinarc_mysql_data"
    assert not Path("deploy/mysql/docker-compose.yml").exists()


def test_sql_profile_uses_the_application_credentials_for_migration() -> None:
    """首次初始化的应用用户必须与一次性迁移使用的用户一致。"""
    compose = _render_sql_compose()
    mysql = compose["services"]["mysql"]["environment"]
    migration = compose["services"]["mysql-migrate"]["environment"]

    assert mysql["MYSQL_USER"] == _MYSQL_TEST_USER
    assert mysql["MYSQL_PASSWORD"] == _MYSQL_TEST_PASSWORD
    assert migration["DATABASE_USERNAME"] == _MYSQL_TEST_USER
    assert migration["DATABASE_PASSWORD"] == _MYSQL_TEST_PASSWORD


def _fake_docker_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """记录启动脚本发出的 Docker 命令，不操作真实容器。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"run --build --rm mysql-migrate"*) exit "${FAKE_MIGRATION_EXIT:-0}" ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["FAKE_DOCKER_LOG"] = str(command_log)
    return environment, command_log


def test_sql_launcher_propagates_migration_failure(tmp_path: Path) -> None:
    """自动 migration 失败时开发启动脚本必须返回相同非零状态。"""
    environment, command_log = _fake_docker_environment(tmp_path)
    environment["FAKE_MIGRATION_EXIT"] = "23"

    completed = subprocess.run(
        ["sh", "deploy/runtime/start.sh", "--profile", "sql"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert completed.returncode == 23
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert any("--profile sql up -d --wait mysql" in command for command in commands)
    assert not any("--profile redis" in command for command in commands)
    assert not any("minio" in command for command in commands)
    assert commands[-1].endswith("run --build --rm mysql-migrate")


def test_default_launcher_starts_nothing(tmp_path: Path) -> None:
    """未选择 profile 时不调用 Docker，也不启动 Agent。"""
    environment, command_log = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        ["sh", "deploy/runtime/start.sh"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert completed.returncode == 0
    assert not command_log.exists()


def test_redis_launcher_starts_only_redis(tmp_path: Path) -> None:
    """选择 Redis 时只启动该中间件，不启动后端。"""
    environment, command_log = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        ["sh", "deploy/runtime/start.sh", "--profile", "redis"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert completed.returncode == 0
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 1
    assert commands[0].endswith("--profile redis up -d --wait redis")


def test_s3_launcher_starts_minio_and_initializes_bucket(tmp_path: Path) -> None:
    """S3 模式只启动 MinIO 并初始化 bucket。"""
    environment, command_log = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        ["sh", "deploy/runtime/start.sh", "--profile", "s3"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert completed.returncode == 0
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 2
    assert commands[0].endswith("--profile s3 up -d --wait minio")
    assert commands[1].endswith("run --rm minio-init")


def test_sql_launcher_can_combine_local_redis(tmp_path: Path) -> None:
    """本地 MySQL 与 Redis 可组合，MySQL 就绪后执行迁移。"""
    environment, command_log = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        [
            "sh",
            "deploy/runtime/start.sh",
            "--profile",
            "redis",
            "--profile",
            "sql",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert completed.returncode == 0
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert commands[0].endswith("--profile redis up -d --wait redis")
    assert commands[1].endswith("--profile sql up -d --wait mysql")
    assert commands[2].endswith("--profile sql run --build --rm mysql-migrate")
    assert len(commands) == 3


def test_launcher_can_select_all_local_dependencies(tmp_path: Path) -> None:
    """Redis、MySQL 和 MinIO 可同时启用，不启动 Agent。"""
    environment, command_log = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        [
            "sh",
            "deploy/runtime/start.sh",
            "--profile",
            "redis",
            "--profile",
            "sql",
            "--profile",
            "s3",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert completed.returncode == 0
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert commands[0].endswith("--profile redis up -d --wait redis")
    assert commands[1].endswith("--profile sql up -d --wait mysql")
    assert commands[2].endswith("--profile sql run --build --rm mysql-migrate")
    assert commands[3].endswith("--profile s3 up -d --wait minio")
    assert commands[4].endswith("--profile s3 run --rm minio-init")
    assert len(commands) == 5


async def test_redis_data_survives_container_recreation() -> None:
    """AOF/RDB volume 必须让值、Stream 和 TTL 跨容器重建恢复。"""
    _require_persistence_opt_in()
    _compose("--profile", "redis", "up", "-d", "--wait", "redis")
    prefix = f"persistence-test-{uuid4().hex}"
    value_key = f"{prefix}:value"
    stream_key = f"{prefix}:stream"
    ttl_key = f"{prefix}:ttl"
    try:
        client = Redis.from_url(_redis_url(), decode_responses=True)
        try:
            await client.set(value_key, "survives")
            entry_id = cast(
                str,
                await client.xadd(stream_key, {"state": "ready"}),
            )
            await client.set(ttl_key, "expires", ex=120)
            await client.waitaof(1, 0, 5_000)
            persistence = cast(
                dict[str, str],
                await client.config_get(
                    "appendonly",
                    "appendfsync",
                    "save",
                    "dir",
                ),
            )
            assert persistence["appendonly"] == "yes"
            assert persistence["appendfsync"] == "everysec"
            assert persistence["dir"] == "/data"
            assert "900 1" in persistence["save"]
            assert "300 10" in persistence["save"]
            assert "60 10000" in persistence["save"]
        finally:
            await client.aclose()

        _compose(
            "--profile", "redis", "up", "-d", "--wait", "--force-recreate", "redis"
        )
        recovered = Redis.from_url(_redis_url(), decode_responses=True)
        try:
            assert await recovered.get(value_key) == "survives"
            entries = cast(
                list[tuple[str, dict[str, str]]],
                await recovered.xrange(stream_key),
            )
            assert entries == [(entry_id, {"state": "ready"})]
            assert await recovered.ttl(ttl_key) > 0
        finally:
            await recovered.aclose()
    finally:
        cleanup = Redis.from_url(_redis_url(), decode_responses=True)
        try:
            await cleanup.delete(value_key, stream_key, ttl_key)
        finally:
            await cleanup.aclose()


async def test_minio_object_survives_container_recreation() -> None:
    """MinIO named volume 必须让精确对象跨容器重建恢复。"""
    _require_persistence_opt_in()
    _compose("--profile", "s3", "up", "-d", "--wait", "minio")
    _compose("--profile", "s3", "run", "--rm", "minio-init")
    settings = _s3_settings()
    _, bucket, _, _, _ = settings
    key = f"persistence-test/{uuid4().hex}"
    content = os.urandom(256)
    try:
        async with _s3_client(settings) as client:
            await client.put_object(Bucket=bucket, Key=key, Body=content)

        _compose("--profile", "s3", "up", "-d", "--wait", "--force-recreate", "minio")
        async with _s3_client(settings) as recovered:
            response = await recovered.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as body:
                assert await body.read() == content
    finally:
        async with _s3_client(settings) as cleanup:
            await cleanup.delete_object(Bucket=bucket, Key=key)
