"""把内部事件白名单投影到可恢复 SessionEventBus。"""

from collections.abc import Mapping

from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.config.models import PublicStreamConfig
from agentsystem.outbound.event_bus import SessionEventBus
from agentsystem.outbound.stream_mapper import to_public_stream_event
from agentsystem.runtime.events import AgentEventEnvelope


class StreamPublicProjector:
    """把白名单公开事件写入可恢复 Stream 的投影器。

    借用共享 EventBus，不负责其连接生命周期。映射或写入异常向上传播，
    由 Session 的统一 fail-close 投影边界处理。
    """

    def __init__(
        self,
        event_bus: SessionEventBus,
        config: PublicStreamConfig,
    ) -> None:
        """保存共享 EventBus 与公开字段策略配置的引用。

        Args:
            event_bus: 负责公开事件追加和恢复读取的共享总线。
            config: 启用开关与公开字段、大小策略。
        """
        self._event_bus = event_bus
        self._config = config

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        """映射并追加公开事件；未启用或未命中白名单时跳过。

        Args:
            ctx: 事件所属 session 的隔离上下文。
            event: 待投影的内部事件。

        Raises:
            ValueError: 事件与上下文的 session_id 不一致。
        """
        if event.session_id != ctx.session_id:
            raise ValueError("Event 与 SessionContext 的 session_id 不一致")
        if not self._config.enabled:
            return
        public_event = to_public_stream_event(event, self._config)
        if public_event is not None:
            await self._event_bus.append(ctx, public_event)


class StreamProjectorFactory:
    """创建使用共享 SessionEventBus 和全局策略的 stream 输出器。

    只保留外部依赖引用，不创建或关闭总线资源。
    """

    def __init__(
        self,
        event_bus: SessionEventBus,
        config: PublicStreamConfig,
    ) -> None:
        """保存 EventBus 与全局公开内容策略。

        Args:
            event_bus: Application 注入的共享事件总线。
            config: 全局公开流配置。
        """
        self._event_bus = event_bus
        self._config = config

    def __call__(
        self,
        options: Mapping[str, JsonValue],
    ) -> StreamPublicProjector:
        """拒绝未知 options 并创建 stream projector。

        Args:
            options: 必须为空；本实现使用工厂保存的全局配置。

        Returns:
            借用同一事件总线的新投影器。

        Raises:
            ValueError: options 非空。
        """
        if options:
            raise ValueError("Stream PublicProjector 不支持 options")
        return StreamPublicProjector(self._event_bus, self._config)
