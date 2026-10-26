"""每 Agent 独立的启动期 capability 表。"""

from typing import cast


class PluginSetupServices:
    """每个 Agent 独立的启动期服务表，按精确类型键查找能力。

    装配层先注册并 freeze，随后插件只读查询。不执行子类匹配或实例类型
    校验，也不创建、关闭所保存的服务对象。
    """

    def __init__(self) -> None:
        """创建尚未冻结的空服务表。"""
        self._items: dict[type[object], object] = {}
        self._frozen = False

    def register[ServiceT](
        self,
        service_type: type[ServiceT],
        value: ServiceT,
    ) -> None:
        """按显式类型键注册唯一能力，不检查 value 的运行时类型。

        Args:
            service_type: 查询时必须使用的精确类型键。
            value: 由装配层创建并管理生命周期的服务实例。

        Raises:
            RuntimeError: 服务表已经冻结。
            ValueError: 同一个类型键重复注册。
        """
        if self._frozen:
            raise RuntimeError("PluginSetupServices 已冻结")
        if service_type in self._items:
            raise ValueError(f"Plugin capability type 重复：{service_type.__name__}")
        self._items[service_type] = value

    def freeze(self) -> None:
        """永久关闭服务表写入口并允许 Plugin 查询。"""
        self._frozen = True

    def get[ServiceT](self, service_type: type[ServiceT]) -> ServiceT | None:
        """按精确类型键查询已冻结的服务表。

        Args:
            service_type: 注册时使用的类型键，不进行继承关系匹配。

        Returns:
            注册实例；缺失或注册值为 None 时返回 None。

        Raises:
            RuntimeError: 服务表尚未冻结。
        """
        self._ensure_frozen()
        value = self._items.get(service_type)
        return None if value is None else cast(ServiceT, value)

    def require[ServiceT](self, service_type: type[ServiceT]) -> ServiceT:
        """查询必须存在的能力。

        Args:
            service_type: 注册时使用的精确类型键。

        Returns:
            非 None 的注册实例。

        Raises:
            RuntimeError: 服务表尚未冻结。
            KeyError: 能力不存在或注册值为 None。
        """
        value = self.get(service_type)
        if value is None:
            raise KeyError(f"缺少 Plugin capability：{service_type.__name__}")
        return value

    @property
    def frozen(self) -> bool:
        """返回服务表是否已永久关闭写入口。

        Returns:
            已调用 freeze 时为 True。
        """
        return self._frozen

    def _ensure_frozen(self) -> None:
        if not self._frozen:
            raise RuntimeError("PluginSetupServices 必须冻结后查询")
