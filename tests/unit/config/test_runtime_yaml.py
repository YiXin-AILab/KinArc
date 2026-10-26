"""第一方 Runtime YAML 的 Manager selector 与 database 插值测试。"""

from pathlib import Path

import pytest
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import RuntimeConfig


def test_sql_runtime_yaml_uses_independent_database_environment(
    tmp_path: Path,
) -> None:
    """SQL selector 独立读取 database，缺失变量明确且不泄漏密码。"""
    path = tmp_path / "runtime.yaml"
    path.write_text(
        """
redis:
  type: standalone
  host: 127.0.0.1
  port: 6379
  database: 0
database:
  type: mysql
  host: ${DATABASE_HOST}
  port: ${DATABASE_PORT}
  name: ${DATABASE_NAME}
  username: ${DATABASE_USERNAME}
  password: ${DATABASE_PASSWORD}
managers:
  message:
    type: sql
  state:
    type: redis
asset:
  max_file_bytes: 104857600
asset_store:
  type: local
""".strip(),
        encoding="utf-8",
    )
    environment = {
        "DATABASE_HOST": "db.internal",
        "DATABASE_PORT": "3306",
        "DATABASE_NAME": "agentsystem",
        "DATABASE_USERNAME": "runtime",
        "DATABASE_PASSWORD": "sql-secret-value",
    }

    config = load_yaml_config(path, RuntimeConfig, environ=environment)

    assert config.managers.message.type == "sql"
    assert config.managers.state.type == "redis"
    assert config.database is not None
    assert config.database.name == "agentsystem"

    del environment["DATABASE_NAME"]
    with pytest.raises(ValueError) as captured:
        load_yaml_config(path, RuntimeConfig, environ=environment)
    assert "DATABASE_NAME" in str(captured.value)
    assert "sql-secret-value" not in str(captured.value)
