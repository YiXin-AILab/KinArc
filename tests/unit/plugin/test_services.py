"""Plugin 启动期 capability 表测试。"""

import pytest
from agentsystem.plugin.services import PluginSetupServices


class _BaseService:
    """测试用 capability type key。"""


class _DerivedService(_BaseService):
    """证明查询不会搜索子类的测试实现。"""


def test_setup_services_resolve_exact_type_keys_after_freeze() -> None:
    """Capability 只按显式注册的精确 type key 查询。"""
    value = _DerivedService()
    services = PluginSetupServices()
    services.register(_BaseService, value)

    with pytest.raises(RuntimeError, match="冻结"):
        services.get(_BaseService)

    services.freeze()

    assert services.get(_BaseService) is value
    assert services.require(_BaseService) is value
    assert services.get(_DerivedService) is None


def test_setup_services_reject_duplicate_and_frozen_writes() -> None:
    """重复 type 和 freeze 后写入不能改变 capability 表。"""
    services = PluginSetupServices()
    services.register(_BaseService, _BaseService())

    with pytest.raises(ValueError, match="重复"):
        services.register(_BaseService, _BaseService())

    services.freeze()

    with pytest.raises(RuntimeError, match="冻结"):
        services.register(_DerivedService, _DerivedService())
    with pytest.raises(KeyError, match="DerivedService"):
        services.require(_DerivedService)
