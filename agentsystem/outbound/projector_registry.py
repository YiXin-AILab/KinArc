"""按稳定类型选择代码注册的 PublicProjector factory。"""

from collections.abc import Callable, Mapping

from agentsystem.common.models import JsonValue
from agentsystem.outbound.projector import PublicProjector

type PublicProjectorFactory = Callable[
    [Mapping[str, JsonValue]],
    PublicProjector,
]


class PublicProjectorRegistry:
    """按 Agent outbound type 选择已注册的 projector factory。

    启动期由代码注册，freeze 后禁止新增；创建操作委托工厂校验配置，
    不允许 YAML 动态导入类，也不托管工厂所创建资源的生命周期。
    """

    def __init__(self) -> None:
        """创建启动期可写的空注册表。"""
        self._items: dict[str, PublicProjectorFactory] = {}
        self._frozen = False

    def register(
        self,
        projector_type: str,
        factory: PublicProjectorFactory,
    ) -> None:
        """注册稳定输出器类型，拒绝重复和冻结后的写入。

        Args:
            projector_type: 非空且未注册的配置名称。
            factory: 接收 options 并创建投影器的可调用对象。

        Raises:
            RuntimeError: 注册表已冻结。
            ValueError: 名称为空或重复。
        """
        if self._frozen:
            raise RuntimeError("PublicProjectorRegistry 已冻结")
        if not projector_type.strip():
            raise ValueError("PublicProjector type 不能为空")
        if projector_type in self._items:
            raise ValueError(f"PublicProjector type 重复：{projector_type}")
        self._items[projector_type] = factory

    def freeze(self) -> None:
        """永久关闭注册入口。"""
        self._frozen = True

    def create(
        self,
        projector_type: str,
        options: Mapping[str, JsonValue],
    ) -> PublicProjector:
        """使用目标 factory 校验 options 并创建 projector。

        Args:
            projector_type: 已注册的配置名称。
            options: 原样传给目标工厂的实现配置。

        Returns:
            工厂构造的公开输出器。

        Raises:
            KeyError: 类型尚未注册；工厂自身异常直接传播。
        """
        try:
            factory = self._items[projector_type]
        except KeyError as error:
            raise KeyError(f"未知 PublicProjector：{projector_type}") from error
        return factory(options)
