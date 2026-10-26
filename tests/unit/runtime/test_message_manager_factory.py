"""Runtime MessageManager adapter 选择的纯单元测试。"""

from typing import cast

import pytest
from agentsystem.config.models import (
    LocalScheduleManagerConfig,
    RedisManagerAdapterConfig,
    RedisScheduleManagerConfig,
    ScheduleManagerConfig,
    SqlManagerAdapterConfig,
    SqlScheduleManagerConfig,
)
from agentsystem.message.redis.manager import RedisMessageManager
from agentsystem.message.sql.manager import SqlMessageManager
from agentsystem.redis.client import RedisClient
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.redis.manager import RedisScheduleManager
from agentsystem.schedule.scheduler import CronSchedule
from agentsystem.schedule.sql.manager import SqlScheduleManager
from agentsystem.service.assembly import (
    _create_message_manager,
    _create_schedule_manager,
    _create_state_manager,
)
from agentsystem.state.redis.manager import RedisStateManager
from agentsystem.state.sql.manager import SqlStateManager
from sqlalchemy.ext.asyncio import AsyncEngine


def test_redis_message_manager_remains_default() -> None:
    """Redis selector 不需要也不触碰 AsyncEngine。"""
    redis_client = cast(RedisClient, object())

    manager = _create_message_manager(
        RedisManagerAdapterConfig(),
        redis_client=redis_client,
        redis_prefix="test",
        database_engine=None,
        operation_timeout_seconds=None,
    )

    assert isinstance(manager, RedisMessageManager)


def test_sql_message_manager_requires_injected_engine() -> None:
    """SQL selector 在装配边界拒绝缺失的应用级 Engine。"""
    with pytest.raises(RuntimeError, match="AsyncEngine"):
        _create_message_manager(
            SqlManagerAdapterConfig(),
            redis_client=cast(RedisClient, object()),
            redis_prefix="test",
            database_engine=None,
            operation_timeout_seconds=5.0,
        )


def test_sql_message_manager_uses_engine_without_taking_ownership() -> None:
    """工厂仅注入 Engine；生命周期由 RuntimeService 外层负责。"""
    engine = cast(AsyncEngine, object())

    manager = _create_message_manager(
        SqlManagerAdapterConfig(),
        redis_client=cast(RedisClient, object()),
        redis_prefix="test",
        database_engine=engine,
        operation_timeout_seconds=5.0,
    )

    assert isinstance(manager, SqlMessageManager)


def test_redis_state_manager_remains_default() -> None:
    """Redis State selector 不需要也不触碰 AsyncEngine。"""
    manager = _create_state_manager(
        RedisManagerAdapterConfig(),
        redis_client=cast(RedisClient, object()),
        redis_prefix="test",
        database_engine=None,
        operation_timeout_seconds=None,
    )

    assert isinstance(manager, RedisStateManager)


def test_sql_state_manager_requires_injected_engine() -> None:
    """SQL State selector 在装配边界拒绝缺失的应用级 Engine。"""
    with pytest.raises(RuntimeError, match="AsyncEngine"):
        _create_state_manager(
            SqlManagerAdapterConfig(),
            redis_client=cast(RedisClient, object()),
            redis_prefix="test",
            database_engine=None,
            operation_timeout_seconds=5.0,
        )


def test_sql_state_manager_uses_shared_engine() -> None:
    """SQL State 与 SQL Message 可接收同一应用级 Engine。"""
    engine = cast(AsyncEngine, object())

    manager = _create_state_manager(
        SqlManagerAdapterConfig(),
        redis_client=cast(RedisClient, object()),
        redis_prefix="test",
        database_engine=engine,
        operation_timeout_seconds=5.0,
    )

    assert isinstance(manager, SqlStateManager)


def test_schedule_manager_selectors_use_only_injected_infrastructure() -> None:
    """Local、Redis、MySQL selector 各自只创建一个明确 adapter。"""
    redis_client = cast(RedisClient, object())
    engine = cast(AsyncEngine, object())
    cron = CronSchedule("Asia/Shanghai")

    def create(config: ScheduleManagerConfig) -> object:
        return _create_schedule_manager(
            config,
            cron=cron,
            redis_client=redis_client,
            redis_prefix="test",
            redis_operation_timeout_seconds=1.0,
            database_engine=engine,
            database_operation_timeout_seconds=2.0,
        )

    assert isinstance(
        create(LocalScheduleManagerConfig()),
        LocalScheduleManager,
    )
    assert isinstance(
        create(RedisScheduleManagerConfig()),
        RedisScheduleManager,
    )
    assert isinstance(
        create(SqlScheduleManagerConfig()),
        SqlScheduleManager,
    )


def test_mysql_schedule_manager_requires_shared_engine() -> None:
    """MySQL Scheduler 不得自行创建数据库连接生命周期。"""
    with pytest.raises(RuntimeError, match="AsyncEngine"):
        _create_schedule_manager(
            SqlScheduleManagerConfig(),
            cron=CronSchedule("Asia/Shanghai"),
            redis_client=cast(RedisClient, object()),
            redis_prefix="test",
            redis_operation_timeout_seconds=1,
            database_engine=None,
            database_operation_timeout_seconds=2,
        )
