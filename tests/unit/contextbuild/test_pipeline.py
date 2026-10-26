"""SystemPrompt、ContextProvider、ContextBuilder 与预算测试。"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    CompactionConfig,
    ModelParamsConfig,
)
from agentsystem.context.builder import ContextBuilder
from agentsystem.context.plugins.datetime_provider import DatetimeProvider
from agentsystem.context.providers import ContextProvider, ContextProviderRunner
from agentsystem.context.system_prompt import SystemPromptBuilder
from agentsystem.message.models import (
    AssetSource,
    CompactionBlock,
    ContextMessage,
    DocumentBlock,
    InjectedBlock,
    InjectedItem,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentsystem.runtime.state import AgentState, SessionStatus
from agentsystem.skill import SkillIndex
from agentsystem.tool.models import ToolDefinition, ToolSource
from agentsystem.tool.registry import ToolRegistry

_NOW = datetime(2026, 8, 3, tzinfo=UTC)


def context() -> SessionContext:
    """返回固定 SessionContext。"""
    return SessionContext(
        app_id="app", user_id="user", agent_id="agent", session_id="s"
    )


def agent_config() -> AgentConfig:
    """返回 SystemPromptBuilder 所需的最小 AgentConfig。"""
    return AgentConfig(
        agent_id="agent",
        agent_name="测试 Agent",
        prompt="unused.yaml",
        outbound=AgentOutboundConfig(
            type="log",
        ),
        llm_model=ModelParamsConfig(model="openai/test"),
    )


def message(
    message_id: str,
    role: Role,
    content: tuple[TextBlock | ToolCallBlock | ToolResultBlock, ...],
    *,
    origin: str,
) -> Message:
    """构造带固定 run 的普通测试 Message。"""
    return Message(
        message_id=message_id,
        session_id="s",
        role=role,
        content=content,
        origin=origin,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=_NOW,
    )


def system_message() -> Message:
    """构造固定 SYSTEM Message。"""
    return Message(
        message_id="system",
        session_id="s",
        role=Role.SYSTEM,
        content=(TextBlock(text="system"),),
        origin=MessageOrigin.SYSTEM,
        visibility=MessageVisibility.MODEL,
        created_at=_NOW,
    )


def running_state(*current_ids: str, steer_ids: tuple[str, ...] = ()) -> AgentState:
    """构造 RUNNING AgentState。"""
    return AgentState(
        session_context=context(),
        status=SessionStatus.RUNNING,
        run_id="run",
        output_mode=OutputMode.COMPLETE,
        started_at=_NOW,
        active_started_at=_NOW,
        current_message_ids=current_ids,
        steer_message_ids=steer_ids,
    )


async def test_system_prompt_has_stable_identity_and_preserves_dynamic_content(
    tmp_path: Any,
) -> None:
    """SYSTEM ID 稳定，Prompt 与工具规则保持原文且不解码已有实体。"""
    skills = SkillIndex.load(roots=(tmp_path,), enabled_skills=())

    class _Handler:
        async def invoke(self, **_: Any) -> Any:
            raise AssertionError("prompt 测试不应调用 handler")

    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="lookup",
            description="lookup",
            parameters={"type": "object", "properties": {}},
            source=ToolSource.BUILTIN,
            rules="仅查询公开数据 <safe> & \"quoted\" 'text' &lt;",
            handler=_Handler(),
        )
    )
    tools.freeze()
    builder = SystemPromptBuilder(
        base_prompt="回答 <用户> 的问题 & \"quoted\" 'text' &lt;",
        tool_registry=tools,
        skill_index=skills,
        clock=lambda: _NOW,
    )

    first = await builder.build(context(), agent_config=agent_config())
    second = await builder.build(context(), agent_config=agent_config())

    assert first == second
    assert first.role is Role.SYSTEM
    assert first.origin == MessageOrigin.SYSTEM
    assert first.run_id is None
    text = first.content[0]
    assert isinstance(text, TextBlock)
    assert "回答 <用户> 的问题 & \"quoted\" 'text' &lt;" in text.text
    assert "lookup: 仅查询公开数据 <safe> & \"quoted\" 'text' &lt;" in text.text
    assert "<safety>" not in text.text


async def test_skill_catalog_exposes_prescanned_resources(tmp_path: Any) -> None:
    """Catalog 提供预扫描资源清单，保留描述和路径中的原始字符。"""
    root = tmp_path / "demo-skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        '---\nname: demo-skill\ndescription: 示例 <policy> & "quotes" &lt;\n---\n正文',
        encoding="utf-8",
    )
    (root / "a&b.md").write_text("关联内容", encoding="utf-8")
    skills = SkillIndex.load(roots=(tmp_path,), enabled_skills=("demo-skill",))
    (root / "new.md").write_text("新内容", encoding="utf-8")
    tools = ToolRegistry()
    tools.freeze()
    builder = SystemPromptBuilder(
        base_prompt="回答问题", tool_registry=tools, skill_index=skills
    )
    catalog = await builder.skill_catalog(context(), agent_config())
    assert '<skill name="demo-skill">' in catalog
    assert '示例 <policy> & "quotes" &lt;' in catalog
    assert '<linked_files>["a&b.md"]</linked_files>' in catalog
    assert "new.md" not in catalog
    assert "正文" not in catalog.split("</skill>")[0]
    assert str(tmp_path) not in catalog


class _Provider(ContextProvider):
    def __init__(
        self, name: str, result: InjectedItem | Exception, delay: float = 0
    ) -> None:
        self._name = name
        self._result = result
        self._delay = delay

    @property
    def provider_name(self) -> str:
        """返回测试 Provider 名称。"""
        return self._name

    async def get(
        self, ctx: SessionContext, *, message: Message
    ) -> InjectedItem | None:
        """返回预设结果、异常或延迟。"""
        del ctx, message
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def put(self, ctx: SessionContext, *, message: Message) -> None:
        """测试 Provider 不消费成功结果。"""
        del ctx, message


async def test_context_provider_runner_requires_atomic_assembly_and_freeze() -> None:
    """非法批次不能部分提交，运行方法只能在 freeze 后调用。"""
    target = message(
        "user",
        Role.USER,
        (TextBlock(text="question"),),
        origin=MessageOrigin.USER,
    )
    valid = _Provider(
        "profile",
        InjectedItem(provider_name="profile", content="VIP"),
    )
    invalid = _Provider("", RuntimeError("must not run"))
    runner = ContextProviderRunner(timeout_seconds=0.1, clock=lambda: _NOW)

    with pytest.raises(RuntimeError, match="冻结"):
        await runner.build_injection(context(), target)
    with pytest.raises(ValueError, match="名称"):
        runner.register_many((valid, invalid))

    runner.register_many((valid,))
    runner.freeze()

    injection = await runner.build_injection(context(), target)
    block = injection.content[0]
    assert isinstance(block, InjectedBlock)
    assert injection.origin == "context_provider"
    assert tuple(item.provider_name for item in block.items) == ("profile",)
    assert runner.frozen is True
    assert runner.timeout_seconds == 0.1
    with pytest.raises(RuntimeError, match="冻结"):
        runner.register_many((valid,))


async def test_frozen_empty_context_provider_runner_is_a_noop() -> None:
    """无 Provider 的 frozen Runner 仍生成空快照并安全忽略 put。"""
    target = message(
        "user",
        Role.USER,
        (TextBlock(text="question"),),
        origin=MessageOrigin.USER,
    )
    final = message(
        "assistant",
        Role.ASSISTANT,
        (TextBlock(text="answer"),),
        origin=MessageOrigin.MODEL,
    )
    runner = ContextProviderRunner(timeout_seconds=0.1, clock=lambda: _NOW)
    runner.freeze()

    injection = await runner.build_injection(context(), target)
    await runner.put(context(), message=final)

    block = injection.content[0]
    assert isinstance(block, InjectedBlock)
    assert block.items == ()


async def test_current_datetime_provider_returns_local_formatted_time() -> None:
    """日期时间 Provider 返回精确到秒的本地时间。"""
    provider = DatetimeProvider(
        clock=lambda: datetime.fromisoformat("2026-08-18T14:05:09+08:00")
    )
    target = message(
        "user",
        Role.USER,
        (TextBlock(text="现在几点"),),
        origin=MessageOrigin.USER,
    )

    item = await provider.get(context(), message=target)

    assert provider.provider_name == "current-datetime"
    assert item == InjectedItem(
        provider_name="current-datetime",
        content="2026-08-18 14:05:09",
    )


async def test_context_provider_runner_puts_to_all_and_isolates_failures() -> None:
    """单个 Provider 回写失败不得阻止其他 Provider 接收最终结果。"""
    calls: list[tuple[str, Message]] = []

    class Provider(ContextProvider):
        def __init__(self, name: str, *, fails: bool = False) -> None:
            self._name = name
            self._fails = fails

        @property
        def provider_name(self) -> str:
            return self._name

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            del ctx, message
            return None

        async def put(self, ctx: SessionContext, *, message: Message) -> None:
            del ctx
            calls.append((self.provider_name, message))
            if self._fails:
                raise RuntimeError("private failure")

    final = message(
        "assistant",
        Role.ASSISTANT,
        (TextBlock(text="answer"),),
        origin=MessageOrigin.MODEL,
    )
    runner = ContextProviderRunner(timeout_seconds=0.1)
    runner.register_many((Provider("broken", fails=True), Provider("memory")))
    runner.freeze()

    await runner.put(context(), message=final)

    assert calls == [("broken", final), ("memory", final)]


async def test_context_provider_runner_put_timeout_is_fail_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """单个 put 超时不阻止其他 Provider 完成回写。"""
    completed: list[str] = []

    class Provider(ContextProvider):
        def __init__(self, name: str, delay: float) -> None:
            self._name = name
            self._delay = delay

        @property
        def provider_name(self) -> str:
            return self._name

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            del ctx, message
            return None

        async def put(self, ctx: SessionContext, *, message: Message) -> None:
            del ctx, message
            await asyncio.sleep(self._delay)
            completed.append(self.provider_name)

    final = message(
        "assistant",
        Role.ASSISTANT,
        (TextBlock(text="answer"),),
        origin=MessageOrigin.MODEL,
    )
    runner = ContextProviderRunner(timeout_seconds=0.001)
    runner.register_many((Provider("slow", 0.05), Provider("fast", 0)))
    runner.freeze()

    with caplog.at_level("WARNING"):
        await runner.put(context(), message=final)

    assert completed == ["fast"]
    timeout = next(
        record
        for record in caplog.records
        if record.message == "context_provider_put_timeout"
    )
    assert timeout.__dict__["provider_name"] == "slow"


async def test_context_provider_runner_isolates_failures_and_keeps_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider 并发执行，失败隔离，结果仍按注册顺序聚合。"""
    target = message(
        "user",
        Role.USER,
        (TextBlock(text="question"),),
        origin=MessageOrigin.USER,
    )
    runner = ContextProviderRunner(timeout_seconds=0.1, clock=lambda: _NOW)
    runner.register_many(
        (
            _Provider("slow", InjectedItem(provider_name="slow", content="A"), 0.01),
            _Provider("broken", RuntimeError("secret")),
            _Provider("fast", InjectedItem(provider_name="fast", content="B")),
        )
    )
    runner.freeze()

    with caplog.at_level("ERROR"):
        injection = await runner.build_injection(context(), target)

    block = injection.content[0]
    assert isinstance(block, InjectedBlock)
    assert [item.provider_name for item in block.items] == ["slow", "fast"]
    assert (
        injection.message_id
        == (await runner.build_injection(context(), target)).message_id
    )
    failures = [
        record
        for record in caplog.records
        if record.getMessage().startswith("context_provider_failed ")
    ]
    assert len(failures) == 2
    assert all(record.exc_info is not None for record in failures)
    assert all("error_id=" in record.getMessage() for record in failures)


async def test_context_provider_timeout_still_produces_empty_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """全部 Provider 超时也生成 items=() 的完成快照。"""
    target = message(
        "user",
        Role.USER,
        (TextBlock(text="question"),),
        origin=MessageOrigin.USER,
    )
    runner = ContextProviderRunner(timeout_seconds=0.001, clock=lambda: _NOW)
    runner.register_many(
        (_Provider("slow", InjectedItem(provider_name="slow", content="A"), 0.05),)
    )
    runner.freeze()

    with caplog.at_level("WARNING"):
        injection = await runner.build_injection(context(), target)

    block = injection.content[0]
    assert isinstance(block, InjectedBlock)
    assert block.items == ()
    timeout = next(
        record
        for record in caplog.records
        if record.message == "context_provider_timeout"
    )
    assert timeout.exc_info is None


async def test_context_provider_rejects_system_without_calling_provider() -> None:
    """动态 SYSTEM 不得触发只面向 USER 的 ContextProvider。"""
    calls = 0

    class Provider(ContextProvider):
        provider_name = "profile"

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            nonlocal calls
            del ctx, message
            calls += 1
            return None

        async def put(self, ctx: SessionContext, *, message: Message) -> None:
            del ctx, message

    target = message(
        "dynamic-system",
        Role.SYSTEM,
        (TextBlock(text="notice"),),
        origin="workflow",
    )
    runner = ContextProviderRunner(timeout_seconds=0.1, clock=lambda: _NOW)
    runner.register_many((Provider(),))
    runner.freeze()

    with pytest.raises(ValueError, match="USER"):
        await runner.build_injection(context(), target)

    assert calls == 0


def test_context_builder_preserves_dynamic_system_order() -> None:
    """动态 SYSTEM 作为 retained history 按原始顺序进入模型上下文。"""
    dynamic_system = message(
        "dynamic-system",
        Role.SYSTEM,
        (TextBlock(text="notice"),),
        origin="workflow",
    )
    user = message(
        "user-1",
        Role.USER,
        (TextBlock(text="question"),),
        origin="human",
    )
    assistant = message(
        "assistant-1",
        Role.ASSISTANT,
        (TextBlock(text="answer"),),
        origin="model",
    )
    source = ContextMessage(
        system_message=system_message(),
        retained_messages=(dynamic_system, user, assistant),
    )

    prepared = ContextBuilder().build(source, running_state("dynamic-system", "user-1"))

    assert [item.message_id for item in prepared] == [
        "system",
        "dynamic-system",
        "user-1",
        "assistant-1",
    ]
    assert prepared[1].role is Role.SYSTEM


def test_context_builder_injects_filters_steer_and_reorders_tool_results() -> None:
    """Builder 注入上下文、排除 STEER，并按 ToolCall 顺序恢复结果。"""
    user = message(
        "user", Role.USER, (TextBlock(text="question"),), origin=MessageOrigin.USER
    )
    call_a = ToolCallBlock(
        tool_call_id="a", tool_name="first", arguments={}, raw_arguments="{}"
    )
    call_b = ToolCallBlock(
        tool_call_id="b", tool_name="second", arguments={}, raw_arguments="{}"
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (call_a, call_b),
        origin=MessageOrigin.MODEL,
    )
    result_b = message(
        "result-b",
        Role.TOOL,
        (ToolResultBlock(tool_call_id="b", tool_name="second", content="B"),),
        origin=MessageOrigin.TOOL,
    )
    result_a = message(
        "result-a",
        Role.TOOL,
        (ToolResultBlock(tool_call_id="a", tool_name="first", content="A"),),
        origin=MessageOrigin.TOOL,
    )
    steer = message(
        "steer", Role.USER, (TextBlock(text="late"),), origin=MessageOrigin.USER
    )
    injection = Message(
        message_id="injection",
        session_id="s",
        role=Role.USER,
        content=(
            InjectedBlock(
                target_user_message_id="user",
                items=(InjectedItem(provider_name='profile"<&', content="VIP <1>"),),
            ),
        ),
        origin=MessageOrigin.CONTEXT_PROVIDER,
        visibility=MessageVisibility.MODEL,
        run_id="run",
        created_at=_NOW,
    )
    source = ContextMessage(
        system_message=system_message(),
        retained_messages=(user, assistant, result_b, result_a, steer),
        injection_messages=(injection,),
    )

    result = ContextBuilder().build(
        source,
        running_state("user", steer_ids=("steer",)),
    )

    assert [item.message_id for item in result] == [
        "system",
        "user",
        "assistant",
        "result-a",
        "result-b",
    ]
    user_text = result[1].content[0]
    assert isinstance(user_text, TextBlock)
    assert "VIP &lt;1&gt;" in user_text.text
    assert user_text.text == (
        '<context_injection>\n<context provider="profile&quot;&lt;&amp;">\n'
        "VIP &lt;1&gt;\n</context>\n</context_injection>"
    )
    original_text = result[1].content[1]
    assert isinstance(original_text, TextBlock)
    assert original_text.text == "question"


def test_context_builder_injects_only_current_run_users() -> None:
    """历史 run 的 Provider 内容不得再次进入模型上下文。"""
    historical = message(
        "historical", Role.USER, (TextBlock(text="old"),), origin=MessageOrigin.USER
    )
    current_first = message(
        "current-first",
        Role.USER,
        (TextBlock(text="first"),),
        origin=MessageOrigin.USER,
    )
    current_last = message(
        "current-last",
        Role.USER,
        (TextBlock(text="last"),),
        origin=MessageOrigin.USER,
    )

    def injection(target: str, content: str) -> Message:
        return Message(
            message_id=f"injection-{target}",
            session_id="s",
            role=Role.USER,
            content=(
                InjectedBlock(
                    target_user_message_id=target,
                    items=(
                        InjectedItem(
                            provider_name="profile",
                            content=content,
                        ),
                    ),
                ),
            ),
            origin=MessageOrigin.CONTEXT_PROVIDER,
            visibility=MessageVisibility.MODEL,
            run_id="run",
            created_at=_NOW,
        )

    source = ContextMessage(
        system_message=system_message(),
        retained_messages=(historical, current_first, current_last),
        injection_messages=(
            injection("historical", "old-context"),
            injection("current-first", "first-context"),
            injection("current-last", "last-context"),
        ),
    )

    result = ContextBuilder().build(
        source,
        running_state("current-first", "current-last"),
    )

    historical_text = result[1].content[0]
    first_injection = result[2].content[0]
    last_injection = result[3].content[0]
    assert isinstance(historical_text, TextBlock)
    assert isinstance(first_injection, TextBlock)
    assert isinstance(last_injection, TextBlock)
    assert historical_text.text == "old"
    assert len(result[1].content) == 1
    assert "first-context" in first_injection.text
    assert "last-context" in last_injection.text


def test_context_builder_rejects_missing_current_message() -> None:
    """current_message_ids 必须全部出现在最终请求中。"""
    source = ContextMessage(system_message=system_message())

    with pytest.raises(ValueError, match="current_message_ids"):
        ContextBuilder().build(source, running_state("missing"))


def test_promoted_steer_is_separate_user_message_after_tool_results() -> None:
    """提升后的 STEER 不修改 ToolResult，而是作为下一条 USER Message。"""
    call = ToolCallBlock(
        tool_call_id="call",
        tool_name="lookup",
        arguments={},
        raw_arguments="{}",
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (call,),
        origin=MessageOrigin.MODEL,
    )
    tool = message(
        "tool",
        Role.TOOL,
        (
            ToolResultBlock(
                tool_call_id="call",
                tool_name="lookup",
                content="原始工具结果",
            ),
        ),
        origin=MessageOrigin.TOOL,
    )
    steer = message(
        "steer",
        Role.USER,
        (TextBlock(text="请重点解释第二项"),),
        origin=MessageOrigin.USER,
    )
    source = ContextMessage(
        system_message=system_message(),
        retained_messages=(assistant, tool, steer),
    )

    result = ContextBuilder().build(source, running_state("steer"))

    assert [item.role for item in result] == [
        Role.SYSTEM,
        Role.ASSISTANT,
        Role.TOOL,
        Role.USER,
    ]
    tool_block = result[2].content[0]
    assert isinstance(tool_block, ToolResultBlock)
    assert tool_block.content == "原始工具结果"
    steer_block = result[3].content[0]
    assert isinstance(steer_block, TextBlock)
    assert steer_block.text == "请重点解释第二项"


def test_context_builder_prunes_only_snapshot_message_ids() -> None:
    """只重放快照列出的工具消息，并保持原始 Message 不变。"""
    large_value = "abcdefgh"
    call = ToolCallBlock(
        tool_call_id="call",
        tool_name="lookup",
        arguments={
            "payload": large_value,
            "nested": [large_value, {"short": "abc"}],
            "count": 8,
        },
        raw_arguments="original raw arguments",
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (call,),
        origin=MessageOrigin.MODEL,
    )
    tool = message(
        "tool",
        Role.TOOL,
        (
            ToolResultBlock(
                tool_call_id="call",
                tool_name="lookup",
                content="12345678",
            ),
        ),
        origin=MessageOrigin.TOOL,
    )
    compaction = Message(
        message_id="compaction",
        session_id="s",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="pruned",
                pruned_message_ids=("assistant", "tool"),
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.ALL,
        model="summary-model",
        run_id="run",
        created_at=_NOW,
    )
    source = ContextMessage(
        system_message=system_message(),
        latest_compaction_message=compaction,
        retained_messages=(assistant, tool),
    )

    original_messages = source.retained_messages
    builder = ContextBuilder(
        CompactionConfig(tool_argument_max_chars=4, tool_result_max_chars=4)
    )
    first = builder.build(source, running_state())
    second = builder.build(source, running_state())

    pruned_call = first[1].content[0]
    pruned_result = first[2].content[0]
    assert isinstance(pruned_call, ToolCallBlock)
    assert isinstance(pruned_result, ToolResultBlock)
    assert pruned_call.tool_call_id == pruned_result.tool_call_id == "call"
    assert pruned_call.tool_name == pruned_result.tool_name == "lookup"
    assert pruned_call.arguments is not None
    marker = (
        '<pruned-tool-content tool_call_id="call" part="arguments" '
        'original_chars="8" sha256="'
        + hashlib.sha256(b"abcdefgh").hexdigest()
        + '">\nabcd\n</pruned-tool-content>'
    )
    assert pruned_call.arguments == {
        "payload": marker,
        "nested": [marker, {"short": "abc"}],
        "count": 8,
    }
    assert pruned_call.raw_arguments == json.dumps(
        pruned_call.arguments,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert pruned_result.content == (
        '<pruned-tool-content tool_call_id="call" part="result" '
        'original_chars="8" sha256="'
        + hashlib.sha256(b"12345678").hexdigest()
        + '">\n1234\n</pruned-tool-content>'
    )
    reconfigured = ContextBuilder(
        CompactionConfig(tool_argument_max_chars=6, tool_result_max_chars=6)
    ).build(source, running_state())
    reconfigured_call = reconfigured[1].content[0]
    assert isinstance(reconfigured_call, ToolCallBlock)
    assert reconfigured_call.arguments is not None
    assert "\nabcdef\n" in str(reconfigured_call.arguments["payload"])
    assert first == second
    assert source.retained_messages == original_messages


def test_recovered_page_pruning_matches_ordinary_tool_result() -> None:
    """恢复页沿用普通结果剪枝，不解析正文或替换为原始调用 ID。"""
    payload = {
        "source": {"tool_call_id": 'original<&"', "part": "result"},
        "range": {"offset": 8, "end_offset": 16, "total_chars": 24, "next_offset": 16},
        "content": "abcd<&ef",
    }
    original = ToolResultBlock(
        tool_call_id="view-call",
        tool_name="tool_content_view",
        content=json.dumps(payload, ensure_ascii=False),
    )
    tool = message("page", Role.TOOL, (original,), origin=MessageOrigin.TOOL)
    builder = ContextBuilder(CompactionConfig(tool_result_max_chars=6))

    pruned = builder._prune_tools((tool,), frozenset(("page",)))[0].content[0]

    assert isinstance(pruned, ToolResultBlock)
    ordinary = original.model_copy(update={"tool_name": "lookup"})
    ordinary_tool = tool.model_copy(update={"content": (ordinary,)})
    ordinary_pruned = builder._prune_tools((ordinary_tool,), frozenset(("page",)))[
        0
    ].content[0]
    assert isinstance(ordinary_pruned, ToolResultBlock)
    assert pruned.content == ordinary_pruned.content
    assert '<pruned-tool-content tool_call_id="view-call"' in pruned.content
    assert original.content[:6] in pruned.content
    assert tool.content[0] == original


def test_recovery_page_metadata_counts_toward_pruning_limit() -> None:
    """恢复页按整体字符数剪枝，短正文和无效 JSON 都不获得特殊处理。"""
    payload = {
        "source": {"tool_call_id": "original", "part": "arguments"},
        "range": {"offset": 0, "end_offset": 2, "total_chars": 2, "next_offset": None},
        "content": "甲乙",
    }
    block = ToolResultBlock(
        tool_call_id="view-call",
        tool_name="tool_content_view",
        content=json.dumps(payload, ensure_ascii=False),
    )
    builder = ContextBuilder(CompactionConfig(tool_result_max_chars=4))
    tool = message("page", Role.TOOL, (block,), origin=MessageOrigin.TOOL)
    pruned = builder._prune_tools((tool,), frozenset(("page",)))[0].content[0]
    assert isinstance(pruned, ToolResultBlock)
    assert '<pruned-tool-content tool_call_id="view-call"' in pruned.content
    assert block.content[:4] in pruned.content

    invalid = block.model_copy(update={"content": "not a JSON page"})
    tool = tool.model_copy(update={"content": (invalid,)})
    pruned = builder._prune_tools((tool,), frozenset(("page",)))[0].content[0]
    assert isinstance(pruned, ToolResultBlock)
    assert '<pruned-tool-content tool_call_id="view-call"' in pruned.content


def test_context_builder_leaves_unlisted_and_short_tool_messages_unchanged() -> None:
    """未列出的消息和未超限内容保持发送语义不变。"""
    call = ToolCallBlock(
        tool_call_id="call",
        tool_name="lookup",
        arguments={"payload": "abc"},
        raw_arguments='{ "payload": "abc" }',
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (call,),
        origin=MessageOrigin.MODEL,
    )
    tool = message(
        "tool",
        Role.TOOL,
        (
            ToolResultBlock(
                tool_call_id="call",
                tool_name="lookup",
                content="abc",
            ),
        ),
        origin=MessageOrigin.TOOL,
    )
    compaction = Message(
        message_id="compaction",
        session_id="s",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="pruned",
                pruned_message_ids=("assistant", "tool"),
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=_NOW,
    )
    result = ContextBuilder(
        CompactionConfig(tool_argument_max_chars=4, tool_result_max_chars=4)
    ).build(
        ContextMessage(
            system_message=system_message(),
            latest_compaction_message=compaction,
            retained_messages=(assistant, tool),
        ),
        running_state(),
    )

    assert result[1] == assistant
    assert result[2] == tool


def test_pure_pruning_block_skips_summary_projection_and_boundary_check() -> None:
    """纯剪枝快照不生成摘要消息，也不声明摘要边界。"""
    user = message(
        "live",
        Role.USER,
        (TextBlock(text="live"),),
        origin=MessageOrigin.USER,
    )
    compaction = Message(
        message_id="compaction",
        session_id="s",
        role=Role.USER,
        content=(CompactionBlock(action="pruned", pruned_message_ids=("missing",)),),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=_NOW,
    )

    result = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            latest_compaction_message=compaction,
            retained_messages=(user,),
        ),
        running_state("live"),
    )

    assert result == (system_message(), user)


def test_context_builder_projects_asset_handles_only_in_sending_copies() -> None:
    """漏掉发送投影或改写权威消息都会破坏句柄与审计边界。"""
    media = DocumentBlock(
        source=AssetSource(asset_id="sha256-document", media_type="application/pdf")
    )
    user = Message(
        message_id="media-user",
        session_id="s",
        role=Role.USER,
        content=(TextBlock(text="read this"), media),
        origin=MessageOrigin.USER,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=_NOW,
    )
    source = ContextMessage(
        system_message=system_message(),
        retained_messages=(user,),
    )
    builder = ContextBuilder(include_asset_id_in_context=True)

    first = builder.build(source, running_state("media-user"))
    second = builder.build(source, running_state("media-user"))

    assert first[1].content == (
        TextBlock(text="read this"),
        TextBlock(
            text=(
                '<asset_reference asset_id="sha256-document" type="document" '
                'media_type="application/pdf" />'
            )
        ),
        media,
    )
    assert first == second
    assert source.retained_messages == (user,)


def test_context_builder_keeps_asset_projection_disabled_by_default() -> None:
    """默认配置不能改变已有模型请求。"""
    media = DocumentBlock(
        source=AssetSource(asset_id="sha256-document", media_type="application/pdf")
    )
    user = Message(
        message_id="media-user",
        session_id="s",
        role=Role.USER,
        content=(media,),
        origin=MessageOrigin.USER,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=_NOW,
    )

    result = ContextBuilder().build(
        ContextMessage(system_message=system_message(), retained_messages=(user,)),
        running_state("media-user"),
    )

    assert result[1] == user


def test_summary_block_still_validates_first_retained_id() -> None:
    """带摘要的快照继续严格校验 retained tail 边界。"""
    user = message(
        "live",
        Role.USER,
        (TextBlock(text="live"),),
        origin=MessageOrigin.USER,
    )
    compaction = Message(
        message_id="compaction",
        session_id="s",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="summarized",
                summary="summary",
                compacted_message_ids=("old",),
                next_retained_message_id="wrong",
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=_NOW,
    )

    with pytest.raises(ValueError, match="retained tail"):
        ContextBuilder().build(
            ContextMessage(
                system_message=system_message(),
                latest_compaction_message=compaction,
                retained_messages=(user,),
            ),
            running_state("live"),
        )


def test_context_builder_repairs_historical_tool_batches() -> None:
    """历史缺失结果可补稳定的失败 ToolResult。"""
    historical_call = ToolCallBlock(
        tool_call_id="old-call",
        tool_name="lookup",
        arguments=None,
        raw_arguments="{broken",
        parse_error="invalid_json",
    )
    historical = Message(
        message_id="old-assistant",
        session_id="s",
        role=Role.ASSISTANT,
        content=(historical_call,),
        origin=MessageOrigin.MODEL,
        visibility=MessageVisibility.ALL,
        run_id="old-run",
        created_at=_NOW,
    )
    repaired = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            retained_messages=(historical,),
        ),
        running_state(),
    )

    repaired_call = repaired[1].content[0]
    repaired_result = repaired[2].content[0]
    assert isinstance(repaired_call, ToolCallBlock)
    assert repaired_call.arguments == {}
    assert repaired_call.raw_arguments == "{}"
    assert isinstance(repaired_result, ToolResultBlock)
    assert repaired_result.tool_call_id == "old-call"
    assert repaired_result.is_error is True


def test_context_builder_synthesizes_current_missing_tool_result() -> None:
    """当前非挂起调用缺少结果时应补失败结果并继续。"""
    call = ToolCallBlock(
        tool_call_id="call",
        tool_name="lookup",
        arguments={"query": "value"},
        raw_arguments='{"query":"value"}',
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (call,),
        origin=MessageOrigin.MODEL,
    )

    result = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            retained_messages=(assistant,),
        ),
        running_state(),
    )

    repaired_result = result[2].content[0]
    assert isinstance(repaired_result, ToolResultBlock)
    assert repaired_result.tool_call_id == "call"
    assert repaired_result.is_error is True
    assert repaired_result.error_code == "tool_result_missing"


def test_context_builder_synthesizes_missing_invalid_argument_result() -> None:
    """当前非法参数缺少结果时应补参数错误并继续。"""
    invalid_call = ToolCallBlock(
        tool_call_id="call",
        tool_name="lookup",
        arguments=None,
        raw_arguments='{"status": enabled}',
        parse_error="Expecting value",
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (invalid_call,),
        origin=MessageOrigin.MODEL,
    )

    result = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            retained_messages=(assistant,),
        ),
        running_state(),
    )

    repaired_call = result[1].content[0]
    repaired_result = result[2].content[0]
    assert isinstance(repaired_call, ToolCallBlock)
    assert repaired_call.arguments == {}
    assert repaired_call.parse_error is None
    assert isinstance(repaired_result, ToolResultBlock)
    assert repaired_result.is_error is True
    assert repaired_result.error_code == "invalid_tool_arguments"


def test_context_builder_drops_current_duplicate_and_orphan_results() -> None:
    """重复及孤立结果只从模型上下文副本中移除。"""
    call = ToolCallBlock(
        tool_call_id="call",
        tool_name="lookup",
        arguments={},
        raw_arguments="{}",
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (call,),
        origin=MessageOrigin.MODEL,
    )
    first = message(
        "first",
        Role.TOOL,
        (ToolResultBlock(tool_call_id="call", tool_name="lookup", content="first"),),
        origin=MessageOrigin.TOOL,
    )
    duplicate = message(
        "duplicate",
        Role.TOOL,
        (
            ToolResultBlock(
                tool_call_id="call",
                tool_name="lookup",
                content="duplicate",
            ),
        ),
        origin=MessageOrigin.TOOL,
    )
    orphan = message(
        "orphan",
        Role.TOOL,
        (
            ToolResultBlock(
                tool_call_id="unknown",
                tool_name="lookup",
                content="orphan",
            ),
        ),
        origin=MessageOrigin.TOOL,
    )

    result = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            retained_messages=(assistant, first, duplicate, orphan),
        ),
        running_state(),
    )

    assert [item.message_id for item in result] == ["system", "assistant", "first"]


def test_context_builder_keeps_pending_tool_call_unresolved() -> None:
    """等待恢复的挂起调用不得被提前补为失败。"""
    call = ToolCallBlock(
        tool_call_id="pending-call",
        tool_name="approval",
        arguments={},
        raw_arguments="{}",
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (call,),
        origin=MessageOrigin.MODEL,
    )
    pending_state = AgentState(
        session_context=context(),
        status=SessionStatus.PENDING,
        run_id="run",
        output_mode=OutputMode.COMPLETE,
        started_at=_NOW,
        pending_tool_call_id="pending-call",
        resume_token="resume-token",
    )

    result = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            retained_messages=(assistant,),
        ),
        pending_state,
    )

    assert result == (system_message(), assistant)


def test_context_builder_keeps_closed_current_invalid_tool_result() -> None:
    """当前参数错误已有 ToolResult 时应反馈模型继续运行。"""
    invalid_call = ToolCallBlock(
        tool_call_id="call",
        tool_name="lookup",
        arguments=None,
        raw_arguments='{"status": enabled}',
        parse_error="Expecting value",
    )
    assistant = message(
        "assistant",
        Role.ASSISTANT,
        (invalid_call,),
        origin=MessageOrigin.MODEL,
    )
    tool = message(
        "tool",
        Role.TOOL,
        (
            ToolResultBlock(
                tool_call_id="call",
                tool_name="lookup",
                content="Expecting value",
                is_error=True,
                error_code="invalid_tool_arguments",
            ),
        ),
        origin=MessageOrigin.TOOL,
    )

    result = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            retained_messages=(assistant, tool),
        ),
        running_state(),
    )

    repaired_call = result[1].content[0]
    assert isinstance(repaired_call, ToolCallBlock)
    assert repaired_call.arguments == {}
    assert repaired_call.raw_arguments == "{}"
    assert repaired_call.parse_error is None
    assert result[2] == tool


def test_context_builder_drops_historical_orphan_result_and_thinking_only() -> None:
    """只能从发送副本中移除历史孤儿结果和纯 thinking 消息。"""
    orphan = Message(
        message_id="orphan",
        session_id="s",
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id="missing",
                tool_name="lookup",
                content="stale",
            ),
        ),
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id="old-run",
        created_at=_NOW,
    )
    thinking = Message(
        message_id="thinking",
        session_id="s",
        role=Role.ASSISTANT,
        content=(ThinkingBlock(thinking="hidden"),),
        origin=MessageOrigin.MODEL,
        visibility=MessageVisibility.ALL,
        run_id="old-run",
        created_at=_NOW,
    )

    result = ContextBuilder().build(
        ContextMessage(
            system_message=system_message(),
            retained_messages=(orphan, thinking),
        ),
        running_state(),
    )

    assert result == (system_message(),)
