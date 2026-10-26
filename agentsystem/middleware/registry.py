"""显式注册、冻结并按范围解析 Middleware。"""

from collections.abc import Sequence

from agentsystem.middleware.models import (
    MiddlewareFailureMode,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
)

_FAIL_CLOSED_POINTS = {
    MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD,
    MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
    MiddlewarePoint.BEFORE_RUN,
    MiddlewarePoint.BEFORE_LLM,
    MiddlewarePoint.AFTER_LLM,
    MiddlewarePoint.BEFORE_TOOL_CALL,
    MiddlewarePoint.BEFORE_COMPACTION,
}


class MiddlewareRegistry:
    """由装配方持有的实例级 Middleware 注册权威。

    启动时校验整批注册，freeze 后只读；GLOBAL 表示适用范围，不表示进程全局单例。
    """

    def __init__(self) -> None:
        """创建尚未冻结的空 Registry。"""
        self._items: dict[str, MiddlewareRegistration] = {}
        self._frozen = False

    def register(self, registration: MiddlewareRegistration) -> None:
        """校验并注册唯一 Middleware。

        Args:
            registration: 待加入当前 Registry 的注册记录。

        Raises:
            RuntimeError: Registry 已冻结。
            ValueError: 注册字段无效，或名称、ID 重复。
        """
        self.register_many((registration,))

    def register_many(
        self,
        registrations: Sequence[MiddlewareRegistration],
    ) -> None:
        """完整校验批次后一次性注册 Middleware。

        Args:
            registrations: 待原子加入内存 Registry 的注册记录。

        Raises:
            RuntimeError: Registry 已冻结。
            ValueError: 任意字段无效或名称、ID 重复；整批均不写入。
        """
        if self._frozen:
            raise RuntimeError("MiddlewareRegistry 已冻结")
        items = tuple(registrations)
        names = {item.name for item in self._items.values()}
        identifiers = set(self._items)
        for item in items:
            if item.name in names:
                raise ValueError(f"Middleware name 重复 name={item.name}")
            if item.middleware_id in identifiers:
                raise ValueError(
                    f"Middleware ID 重复 middleware_id={item.middleware_id}"
                )
            names.add(item.name)
            identifiers.add(item.middleware_id)
        for item in items:
            _validate_registration(item)
        self._items.update((item.middleware_id, item) for item in items)

    def freeze(self) -> None:
        """永久关闭当前 Registry 的写入口。"""
        self._frozen = True

    def resolve(
        self,
        *,
        app_id: str,
        point: MiddlewarePoint,
    ) -> tuple[MiddlewareRegistration, ...]:
        """返回匹配范围的注册记录，按优先级降序、ID 升序排列。

        Args:
            app_id: 当前应用 ID，用于匹配 APP 范围。
            point: 本次要执行的挂点。

        Returns:
            全局和当前应用命中的注册记录元组。
        """
        matches = (
            item
            for item in self._items.values()
            if item.point is point
            and (
                item.scope is MiddlewareScope.GLOBAL
                or (item.scope is MiddlewareScope.APP and item.app_id == app_id)
            )
        )
        return tuple(
            sorted(matches, key=lambda item: (-item.priority, item.middleware_id))
        )

    @property
    def frozen(self) -> bool:
        """返回 Registry 是否完成启动期冻结。"""
        return self._frozen


def _validate_registration(registration: MiddlewareRegistration) -> None:
    if not registration.name.strip() or registration.name != registration.name.strip():
        raise ValueError("Middleware name 不能为空或包含首尾空白")
    if not registration.middleware_id.strip():
        raise ValueError("middleware_id 不能为空")
    if registration.timeout_ms <= 0:
        raise ValueError("Middleware timeout_ms 必须为正数")
    if registration.scope is MiddlewareScope.GLOBAL and registration.app_id is not None:
        raise ValueError("GLOBAL Middleware 不能配置 app_id")
    if registration.scope is MiddlewareScope.APP and not registration.app_id:
        raise ValueError("APP Middleware 必须配置 app_id")
    if (
        registration.failure_mode is MiddlewareFailureMode.FAIL_CLOSED
        and registration.point not in _FAIL_CLOSED_POINTS
    ):
        raise ValueError(f"{registration.point.value} 不允许 FAIL_CLOSED")
