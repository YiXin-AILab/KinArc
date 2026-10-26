"""Todo 配置与共享后端选择。"""

import pytest
from agentsystem.config.models import RuntimeConfig
from agentsystem.service.assembly import _create_todo_store
from agentsystem.todo.local.store import LocalTodoStore
from agentsystem.todo.redis.store import RedisTodoStore
from redis.asyncio import Redis
from tests.unit.config.test_models import runtime_data


def test_todo_selectors_and_no_enabled() -> None:
    data = runtime_data()
    assert RuntimeConfig.model_validate(data).todo.store.type == "redis"
    for kind, expected in (("local", LocalTodoStore), ("redis", RedisTodoStore)):
        data["todo"] = {"store": {"type": kind}}
        store = _create_todo_store(
            RuntimeConfig.model_validate(data),
            redis_client=Redis(),
            database_engine=None,
        )
        assert isinstance(store, expected)
    for config in (
        {"enabled": True},
        {"store": {"type": "sqlite"}},
        {"store": {"type": "db"}},
    ):
        data["todo"] = config
        with pytest.raises(ValueError):
            RuntimeConfig.model_validate(data)


def test_todo_is_only_database_consumer() -> None:
    data = runtime_data()
    data["todo"] = {"store": {"type": "db"}}
    data["database"] = {
        "type": "mysql",
        "host": "localhost",
        "name": "test",
        "username": "test",
    }
    assert RuntimeConfig.model_validate(data).database is not None
