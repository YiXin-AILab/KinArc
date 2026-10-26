"""Memory selector 与数据库资源校验。"""

import pytest
from agentsystem.config.models import RuntimeConfig
from agentsystem.memory.local.store import LocalMemoryStore
from agentsystem.memory.redis.store import RedisMemoryStore
from agentsystem.service.assembly import _create_memory_store
from pydantic import ValidationError
from redis.asyncio import Redis
from tests.unit.config.test_models import runtime_data


def test_disabled_and_selected_store() -> None:
    data = runtime_data()
    for enabled, kind, expected in (
        (False, "redis", type(None)),
        (True, "local", LocalMemoryStore),
        (True, "redis", RedisMemoryStore),
    ):
        data["memory"] = {"enabled": enabled, "store": {"type": kind}}
        store = _create_memory_store(
            RuntimeConfig.model_validate(data),
            redis_client=Redis(),
            database_engine=None,
        )
        assert isinstance(store, expected)


def test_memory_only_database_consumer() -> None:
    data = runtime_data()
    data["memory"] = {"enabled": True, "store": {"type": "db"}}
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)
    data["database"] = {
        "type": "mysql",
        "host": "localhost",
        "name": "test",
        "username": "test",
    }
    config = RuntimeConfig.model_validate(data)
    assert config.database is not None
