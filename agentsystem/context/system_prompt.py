"""使用固定分区构建 session 唯一 SYSTEM Message。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid5

from agentsystem.common.models import SessionContext
from agentsystem.config.models import AgentConfig
from agentsystem.memory.service import MemoryService
from agentsystem.memory.store import MemoryScope
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecisionType,
    MiddlewarePoint,
    SystemPromptBuildPayload,
)
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.registry import ToolRegistry

_SYSTEM_NAMESPACE = UUID("ac3eb61e-1c60-4fac-b0f9-93042d94ef8d")


def system_message_id(ctx: SessionContext) -> str:
    """根据 session 身份生成跨进程稳定的 SYSTEM Message ID。

    Args:
        ctx: 包含应用、Agent 和 session 标识的会话上下文。

    Returns:
        不依赖 run 或当前时间的 UUID5 字符串。
    """
    identity = f"{ctx.app_id}/{ctx.agent_id}/{ctx.session_id}"
    return str(uuid5(_SYSTEM_NAMESPACE, identity))


class SystemPromptBuilder:
    """在 session 初始化时将原始正文按稳定顺序放入非空协议分区。

    使用注入的工具、Skill、Memory 和 Middleware；仅构造消息，幂等保存由调用方负责。
    分段方法可由子类覆盖，before/after UPDATE 可提供完整替换正文。
    """

    def __init__(
        self,
        *,
        base_prompt: str,
        tool_registry: ToolRegistry,
        skill_index: SkillIndex,
        clock: Callable[[], datetime] | None = None,
        middleware_manager: MiddlewareManager | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        """保存已初始化的资产和可选服务。

        Args:
            base_prompt: 非空的 Agent 基础指令正文。
            tool_registry: 提供工具使用规则的 Registry。
            skill_index: 提供已启用 Skill 目录的索引。
            clock: SYSTEM 消息创建时间工厂，缺省使用 UTC 当前时间。
            middleware_manager: 可选的系统提示词构建挂点管理器。
            memory_service: 可选的用户记忆读取服务。

        Raises:
            ValueError: 基础指令为空白。
        """
        if not base_prompt.strip():
            raise ValueError("基础 System Prompt 不能为空")
        self._base_prompt = base_prompt
        self._tools = tool_registry
        self._skills = skill_index
        self._clock = clock or (lambda: datetime.now(UTC))
        self._middleware = middleware_manager
        self._memory = memory_service

    async def build(
        self,
        ctx: SessionContext,
        *,
        agent_config: AgentConfig,
    ) -> Message:
        """执行构建挂点并生成带确定性 ID 的 SYSTEM Message。

        Args:
            ctx: 待初始化的会话身份，不需要 run 标识。
            agent_config: 传给分段扩展方法的 Agent 配置。

        Returns:
            尚未持久化、仅模型可见且不带 run_id 的 SYSTEM Message。

        Memory 读取和 fail-closed Middleware 异常直接传播，由初始化边界处理。
        """
        prompt = await self._hook(
            ctx, MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD, self._base_prompt
        )
        if prompt is None:
            prompt = await self._default_prompt(ctx, agent_config)
        replacement = await self._hook(
            ctx, MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD, prompt
        )
        if replacement is not None:
            prompt = replacement
        return Message(
            message_id=system_message_id(ctx),
            session_id=ctx.session_id,
            role=Role.SYSTEM,
            content=(TextBlock(text=prompt),),
            origin=MessageOrigin.SYSTEM,
            visibility=MessageVisibility.MODEL,
            created_at=self._clock(),
        )

    async def _hook(
        self, ctx: SessionContext, point: MiddlewarePoint, prompt: str
    ) -> str | None:
        if self._middleware is None:
            return None
        decision = await self._middleware.dispatch(
            MiddlewareContext(
                invocation_id="",
                point=point,
                ctx=ctx,
                run_id=None,
                trace_id=None,
                state_snapshot=None,
                payload=SystemPromptBuildPayload(prompt=prompt),
            )
        )
        if decision.type is MiddlewareDecisionType.UPDATE:
            if not isinstance(decision.payload, SystemPromptBuildPayload):
                raise ValueError("system prompt UPDATE requires a prompt payload")
            return decision.payload.prompt
        return None

    async def _default_prompt(
        self, ctx: SessionContext, agent_config: AgentConfig
    ) -> str:
        required_section = _section(
            "agent_prompt", await self.base_prompt(ctx, agent_config)
        )
        optional_sections = (
            _optional_section("tool_rules", await self.tool_rules(ctx, agent_config)),
            _optional_section(
                "skill_catalog", await self.skill_catalog(ctx, agent_config)
            ),
            _optional_section("sub_agents", await self.sub_agents(ctx, agent_config)),
            _optional_section("user_memory", await self.user_memory(ctx)),
            _optional_section("agent_memory", await self.agent_memory(ctx)),
            _optional_section("safety", await self.safety(ctx, agent_config)),
            _optional_section(
                "model_guidance",
                await self.model_guidance(ctx, agent_config),
            ),
        )
        sections = (
            required_section,
            *(section for section in optional_sections if section is not None),
        )
        return "\n\n".join(sections)

    async def base_prompt(self, ctx: SessionContext, agent_config: AgentConfig) -> str:
        """返回未经 HTML/XML 转义的 Agent 基础指令。

        Args:
            ctx: 子类扩展可使用的会话身份，默认实现忽略。
            agent_config: 子类扩展可使用的 Agent 配置，默认实现忽略。

        Returns:
            构造时保存的基础指令原文。
        """
        del ctx, agent_config
        return self._base_prompt

    async def tool_rules(self, ctx: SessionContext, agent_config: AgentConfig) -> str:
        """只渲染 Tool rules，不向 SYSTEM 复制 Schema。

        Args:
            ctx: 子类扩展可使用的会话身份，默认实现忽略。
            agent_config: 子类扩展可使用的 Agent 配置，默认实现忽略。

        Returns:
            按工具注册顺序排列的非空规则列表文本。
        """
        del ctx, agent_config
        return "\n".join(f"- {name}: {rule}" for name, rule in self._tools.rules())

    async def skill_catalog(
        self, ctx: SessionContext, agent_config: AgentConfig
    ) -> str:
        """渲染紧凑目录和渐进式披露规则。

        Args:
            ctx: 子类扩展可使用的会话身份，默认实现忽略。
            agent_config: 子类扩展可使用的 Agent 配置，默认实现忽略。

        Returns:
            包含名称、说明和关联路径的目录；无已启用 Skill 时返回空串。
        """
        del ctx, agent_config
        entries = self._skills.catalog()
        lines = [
            f'- <skill name="{item.name}">'
            f"{item.description}"
            "<linked_files>"
            f"{json.dumps(item.linked_files, ensure_ascii=False)}"
            "</linked_files></skill>"
            for item in entries
        ]
        if entries:
            lines.extend(
                (
                    "Skill 是任务说明而不是可直接调用的工具。",
                    "匹配 Skill 时传 name 调用 skill_view，逐页读取完整 SKILL.md。",
                    "按 linked_files 或正文引用路径按需读取关联资源。",
                )
            )
        return "\n".join(lines)

    async def sub_agents(self, ctx: SessionContext, agent_config: AgentConfig) -> str:
        """返回当前版本的空 Sub-agent 快照。

        Args:
            ctx: 为分段扩展保留的会话身份。
            agent_config: 为分段扩展保留的 Agent 配置。

        Returns:
            空字符串，因此默认不生成 sub_agents 分区。
        """
        del ctx, agent_config
        return ""

    async def user_memory(self, ctx: SessionContext) -> str:
        """读取当前用户跨 Agent 的完整记忆原文。

        Args:
            ctx: 包含记忆所属用户身份的会话上下文。

        Returns:
            USER_GLOBAL 记忆；服务未配置或记忆不存在时返回空串。
        """
        return await self._memory_section(ctx, MemoryScope.USER_GLOBAL)

    async def agent_memory(self, ctx: SessionContext) -> str:
        """读取当前用户与 Agent 绑定的完整记忆原文。

        Args:
            ctx: 包含用户和 Agent 身份的会话上下文。

        Returns:
            USER_AGENT 记忆；服务未配置或记忆不存在时返回空串。
        """
        return await self._memory_section(ctx, MemoryScope.USER_AGENT)

    async def _memory_section(self, ctx: SessionContext, scope: MemoryScope) -> str:
        if self._memory is None:
            return ""
        memory = await self._memory.context(ctx, scope)
        if memory is None:
            return ""
        return memory

    async def safety(self, ctx: SessionContext, agent_config: AgentConfig) -> str:
        """提供可选安全指令的分段扩展点。

        Args:
            ctx: 子类扩展可使用的会话身份，默认实现忽略。
            agent_config: 子类扩展可使用的 Agent 配置，默认实现忽略。

        Returns:
            默认返回空串，不生成 safety 分区。
        """
        del ctx, agent_config
        return ""

    async def model_guidance(
        self, ctx: SessionContext, agent_config: AgentConfig
    ) -> str:
        """提供模型供应商或模型家族特殊指令的扩展点。

        Args:
            ctx: 子类扩展可使用的会话身份，默认实现忽略。
            agent_config: 子类扩展可使用的 Agent 配置，默认实现忽略。

        Returns:
            默认返回空串，不生成 model_guidance 分区。
        """
        del ctx, agent_config
        return ""


def _section(name: str, content: str) -> str:
    return f"<{name}>\n{content}\n</{name}>"


def _optional_section(name: str, content: str) -> str | None:
    """仅为非空正文生成协议分段。"""
    if not content.strip():
        return None
    return _section(name, content)
