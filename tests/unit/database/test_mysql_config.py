"""真实 MySQL 测试配置的 opt-in 单元测试。"""

import pytest
from tests.mysql_config import require_local_mysql_config


def test_mysql_test_config_skips_without_runtime_password() -> None:
    """未显式提供本地数据库凭证时不得尝试外部连接。"""
    with pytest.raises(pytest.skip.Exception):
        require_local_mysql_config({})


def test_mysql_test_config_uses_only_runtime_environment() -> None:
    """启用真实测试时只读取唯一的 Runtime MySQL 变量组。"""
    config = require_local_mysql_config(
        {
            "RUNTIME_MYSQL_HOST": "database.test",
            "RUNTIME_MYSQL_HOST_PORT": "4406",
            "RUNTIME_MYSQL_DATABASE": "runtime_contract",
            "RUNTIME_MYSQL_USER": "contract_user",
            "RUNTIME_MYSQL_PASSWORD": "contract-password",
        }
    )

    assert config.host == "database.test"
    assert config.port == 4406
    assert config.name == "runtime_contract"
    assert config.username == "contract_user"
    assert config.password is not None
    assert config.password.get_secret_value() == "contract-password"
