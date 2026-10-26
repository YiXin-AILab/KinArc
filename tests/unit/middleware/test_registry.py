"""Middleware Registry 批量注册测试。"""

import pytest
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
)
from agentsystem.middleware.registry import MiddlewareRegistry


def test_register_many_is_atomic() -> None:
    """批次中任一 Middleware 非法时 Registry 保持调用前状态。"""

    async def proceed(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    valid = MiddlewareRegistration(
        name="valid",
        middleware_id="valid",
        point=MiddlewarePoint.BEFORE_RUN,
        scope=MiddlewareScope.GLOBAL,
        handler=proceed,
    )
    invalid = MiddlewareRegistration(
        name="invalid",
        middleware_id="invalid",
        point=MiddlewarePoint.BEFORE_RUN,
        scope=MiddlewareScope.GLOBAL,
        handler=proceed,
        timeout_ms=0,
    )
    registry = MiddlewareRegistry()

    with pytest.raises(ValueError, match="timeout_ms"):
        registry.register_many((valid, invalid))

    assert registry.resolve(app_id="app", point=MiddlewarePoint.BEFORE_RUN) == ()
    registry.register_many((valid,))
    registry.freeze()
    assert tuple(
        item.middleware_id
        for item in registry.resolve(app_id="app", point=MiddlewarePoint.BEFORE_RUN)
    ) == ("valid",)


def test_register_many_rejects_duplicate_names_atomically() -> None:
    """不同内部 ID 不能共享同一启用列表名称。"""

    async def proceed(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    registry = MiddlewareRegistry()
    registrations = (
        MiddlewareRegistration(
            name="audit",
            middleware_id="first",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            handler=proceed,
        ),
        MiddlewareRegistration(
            name="audit",
            middleware_id="second",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            handler=proceed,
        ),
    )

    with pytest.raises(ValueError, match="Middleware name 重复"):
        registry.register_many(registrations)

    assert registry.resolve(app_id="app", point=MiddlewarePoint.BEFORE_RUN) == ()


@pytest.mark.parametrize(
    "point",
    (MiddlewarePoint.BEFORE_TURN, MiddlewarePoint.AFTER_TURN),
)
def test_turn_points_reject_fail_closed(point: MiddlewarePoint) -> None:
    """Turn 提醒故障不能自动升级成任务阻断。"""

    async def proceed(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    registry = MiddlewareRegistry()
    with pytest.raises(ValueError, match="不允许 FAIL_CLOSED"):
        registry.register(
            MiddlewareRegistration(
                name=f"closed-{point.value}",
                middleware_id=f"closed-{point.value}",
                point=point,
                scope=MiddlewareScope.GLOBAL,
                handler=proceed,
                failure_mode=MiddlewareFailureMode.FAIL_CLOSED,
            )
        )
