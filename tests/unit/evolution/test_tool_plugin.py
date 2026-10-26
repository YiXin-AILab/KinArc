"""Evolution 专属 Tool Plugin 注册测试。"""

from pathlib import Path
from threading import Event, get_ident

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.message.models import ToolCallBlock
from agentsystem.plugin.assembler import assemble_agent_capabilities
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.context import ToolInvocationContext
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolDefinition, ToolExecutionResult, ToolOutput
from agentsystem.tool.registry import ToolRegistry
from evolution.agent.plugins.evidence_tools import EvidenceTools
from evolution.agent.plugins.evolution_tools import (
    ToolThreads,
    create_evolution_tool_plugin,
)
from evolution.agent.plugins.file_tools import WorkspaceTools, _atomic_write
from evolution.diagnosis import build_diagnosis_brief
from evolution.improver import ImprovementRequest
from evolution.models import CaseEvaluation, EvaluationResult, ExecutionMetadata
from pydantic import ValidationError

_TOOL_NAMES = (
    "list_path",
    "read_file",
    "search_path",
    "write_file",
    "make_directory",
    "move_path",
    "delete_path",
    "read_case_evidence",
    "read_case_tracelog",
)


async def test_timed_out_file_write_blocks_following_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时线程停止前不能启动下一次写入，等待超时也不能留下排队写入。"""
    release = Event()
    path = tmp_path / "value.txt"
    path.write_text("initial")
    evaluation = EvaluationResult(total_score=0, cases=())
    files = WorkspaceTools()
    files.bind(
        ImprovementRequest(
            objective="test",
            base_candidate_id="seed",
            changelog_source_candidate_id="seed",
            evidence_candidate_id="seed",
            output_candidate_id="next",
            evidence_parent_candidate_id=None,
            workspace_dir=tmp_path,
            changelog_path=tmp_path / "changelog.md",
            read_only=(),
            train_evaluation=evaluation,
            diagnosis_brief=build_diagnosis_brief("seed", evaluation),
        )
    )
    threads = ToolThreads()
    capabilities = assemble_agent_capabilities(
        agent_id="evolution-agent",
        sources=(create_evolution_tool_plugin(files, EvidenceTools(), threads),),
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=("write_file",),
        enabled_middleware_patterns=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1,
        max_content_chars=100_000,
    )
    executor = ToolExecutor(capabilities.tool_registry, timeout_seconds=0.05)
    context = ToolInvocationContext(
        session_context=SessionContext(
            app_id="test", user_id="u", agent_id="a", session_id="s"
        ),
        run_id="r",
        runtime_trace_id="t",
        turn_index=1,
        tool_call_id="c",
        tool_name="write_file",
    )

    def delayed_write(destination: Path, content: bytes) -> None:
        if content == b"old-write":
            assert release.wait(5)
        _atomic_write(destination, content)

    async def write(text: str) -> ToolExecutionResult:
        result = await executor.execute(
            context,
            ToolCallBlock(
                tool_call_id=text,
                tool_name="write_file",
                raw_arguments="{}",
                arguments={
                    "action": "overwrite",
                    "path": "value.txt",
                    "new_content": text,
                },
            ),
        )
        assert isinstance(result, ToolExecutionResult)
        return result

    monkeypatch.setattr(
        "evolution.agent.plugins.file_tools._atomic_write", delayed_write
    )
    try:
        assert (await write("old-write")).block.error_code == "tool_timeout"
        assert (await write("must-not-run")).block.error_code == "tool_timeout"
        assert path.read_text() == "initial"
    finally:
        release.set()
        await threads.wait()
    assert path.read_text() == "old-write"
    assert (await write("new-write")).block.error_code is None
    await threads.wait()
    assert path.read_text() == "new-write"


class _ThreadReportingWorkspaceTools(WorkspaceTools):
    """记录文件 handler 实际执行线程。"""

    def __init__(self) -> None:
        super().__init__()
        self.thread_id: int | None = None

    def list_path(self, path: str = ".", max_depth: int = 1) -> ToolOutput:
        self.thread_id = get_ident()
        return super().list_path(path, max_depth)


def _definitions() -> dict[str, ToolDefinition]:
    capabilities = assemble_agent_capabilities(
        agent_id="evolution-agent",
        sources=(create_evolution_tool_plugin(WorkspaceTools(), EvidenceTools()),),
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=_TOOL_NAMES,
        enabled_middleware_patterns=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000_000,
    )
    registry = capabilities.tool_registry
    return {
        name: definition
        for name in registry.names()
        if (definition := registry.get(name)) is not None
    }


def test_plugin_registers_only_the_evolution_tools() -> None:
    """旧名或多余工具进入 Registry 会扩大 Agent 能力边界。"""
    definitions = _definitions()

    assert tuple(definitions) == _TOOL_NAMES
    assert all(definition.rules is None for definition in definitions.values())
    assert all(not definition.can_suspend for definition in definitions.values())
    assert all(not definitions[name].is_concurrency_safe for name in _TOOL_NAMES[:7])
    assert all(definitions[name].is_concurrency_safe for name in _TOOL_NAMES[7:])


@pytest.mark.parametrize(
    ("name", "arguments", "required"),
    [
        ("list_path", {}, None),
        ("read_file", {"path": "file.txt"}, "path"),
        ("search_path", {"path": ".", "query": "needle"}, "query"),
        (
            "write_file",
            {"action": "create", "path": "file.txt", "new_content": "text"},
            "action",
        ),
        (
            "write_file",
            {"action": "append", "path": "file.txt", "new_content": "more"},
            "action",
        ),
        (
            "write_file",
            {"action": "prepend", "path": "file.txt", "new_content": "new first"},
            "action",
        ),
        ("make_directory", {"path": "folder"}, "path"),
        ("move_path", {"source": "old", "destination": "new"}, "destination"),
        ("delete_path", {"path": "file.txt"}, "path"),
        ("read_case_evidence", {"case_id": "case-1"}, "case_id"),
        ("read_case_tracelog", {"case_id": "case-1"}, "case_id"),
        ("read_case_tracelog", {"case_id": "case-1", "limit": None}, "case_id"),
    ],
)
def test_tool_arguments_accept_valid_input_and_reject_incomplete_or_extra_input(
    name: str,
    arguments: dict[str, object],
    required: str | None,
) -> None:
    """验证注册模型实际执行校验，不复制完整 Schema 或描述文案。"""
    model = _definitions()[name].invocation_model
    assert model is not None
    model.model_validate(arguments)

    with pytest.raises(ValidationError):
        model.model_validate({**arguments, "unexpected": True})
    if required is not None:
        incomplete = {key: value for key, value in arguments.items() if key != required}
        with pytest.raises(ValidationError):
            model.model_validate(incomplete)


async def test_plugin_runs_workspace_file_io_outside_event_loop(
    tmp_path: Path,
) -> None:
    """同步文件 handler 不得阻塞 Runtime 事件循环。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    files = _ThreadReportingWorkspaceTools()
    files.bind(
        ImprovementRequest(
            objective="improve",
            base_candidate_id="candidate-1",
            changelog_source_candidate_id="candidate-1",
            evidence_candidate_id="candidate-1",
            output_candidate_id="candidate-output",
            evidence_parent_candidate_id=None,
            workspace_dir=workspace,
            read_only=(),
            train_evaluation=EvaluationResult(total_score=0, cases=()),
            diagnosis_brief=build_diagnosis_brief(
                "candidate-1", EvaluationResult(total_score=0, cases=())
            ),
            changelog_path=tmp_path / "changelog.md",
        )
    )
    capabilities = assemble_agent_capabilities(
        agent_id="evolution-agent",
        sources=(create_evolution_tool_plugin(files, EvidenceTools()),),
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=("list_path",),
        enabled_middleware_patterns=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000_000,
    )
    definition = capabilities.tool_registry.get("list_path")
    assert definition is not None

    await definition.handler.invoke(ctx=object(), arguments={})

    assert files.thread_id is not None
    assert files.thread_id != get_ident()


@pytest.mark.parametrize(
    "arguments",
    [
        {"case_id": ""},
        {"case_id": 1},
        {"case_id": "case-1", "offset": True},
        {"case_id": "case-1", "limit": True},
        {"case_id": "case-1", "truncate": "false"},
        {"case_id": "case-1", "role": "validation"},
    ],
)
def test_case_tracelog_schema_rejects_invalid_arguments(
    arguments: dict[str, object],
) -> None:
    """参数校验不得把 bool 转成行数或允许选择其他 Dataset Role。"""
    model = _definitions()["read_case_tracelog"].invocation_model
    assert model is not None
    with pytest.raises(ValidationError):
        model.model_validate(arguments)


@pytest.mark.parametrize(
    ("offset", "limit", "code"),
    [
        (-1, 1, "invalid_offset"),
        (0, 0, "invalid_limit"),
    ],
)
async def test_tool_executor_preserves_page_error_codes(
    offset: int,
    limit: int,
    code: str,
) -> None:
    """范围错误必须进入 Evidence 校验，不能被改写为通用参数错误码。"""
    registry = ToolRegistry()
    registry.register(_definitions()["read_case_tracelog"])
    registry.freeze()
    call = ToolCallBlock(
        tool_call_id="call-1",
        tool_name="read_case_tracelog",
        raw_arguments="{}",
        arguments={"case_id": "case-1", "offset": offset, "limit": limit},
    )
    context = ToolInvocationContext(
        session_context=SessionContext(
            app_id="test",
            user_id="user",
            agent_id="agent",
            session_id="session",
        ),
        run_id="run-1",
        runtime_trace_id="trace-1",
        turn_index=1,
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
    )
    result = await ToolExecutor(registry).execute(context, call)
    assert isinstance(result, ToolExecutionResult)
    assert result.block.error_code == code


class _ThreadReportingEvidenceTools(EvidenceTools):
    """记录真实视图构造所在的线程。"""

    def __init__(self) -> None:
        super().__init__()
        self.thread_id: int | None = None

    def _case_view(self, case_id: str) -> tuple[CaseEvaluation, tuple[str, ...]]:
        self.thread_id = get_ident()
        return super()._case_view(case_id)


@pytest.mark.parametrize("name", ["read_case_evidence", "read_case_tracelog"])
async def test_case_tools_build_views_outside_event_loop(
    tmp_path: Path,
    name: str,
) -> None:
    """Case 视图生成不得在事件循环中进行，也不能把文本结果再包成 JSON。"""
    evidence = _ThreadReportingEvidenceTools()
    evidence.bind(
        ImprovementRequest(
            objective="improve",
            base_candidate_id="candidate-1",
            changelog_source_candidate_id="candidate-1",
            evidence_candidate_id="candidate-1",
            output_candidate_id="candidate-output",
            evidence_parent_candidate_id=None,
            workspace_dir=tmp_path,
            read_only=(),
            changelog_path=tmp_path / "changelog.md",
            train_evaluation=EvaluationResult(
                total_score=0,
                cases=(
                    CaseEvaluation(
                        id="case-1",
                        result="failed",
                        tracelog="original\n" * 600,
                        metadata=ExecutionMetadata(total_tokens=0, duration_seconds=0),
                        expected={},
                        score=0,
                        feedback="",
                    ),
                ),
            ),
            diagnosis_brief=build_diagnosis_brief(
                "candidate-1", EvaluationResult(total_score=0, cases=())
            ),
        )
    )
    capabilities = assemble_agent_capabilities(
        agent_id="evolution-agent",
        sources=(create_evolution_tool_plugin(WorkspaceTools(), evidence),),
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=(name,),
        enabled_middleware_patterns=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000_000,
    )
    definition = capabilities.tool_registry.get(name)
    assert definition is not None
    result = await definition.handler.invoke(
        ctx=object(), arguments={"case_id": "case-1"}
    )
    assert evidence.thread_id is not None and evidence.thread_id != get_ident()
    if name == "read_case_tracelog":
        assert result.content.startswith(
            "--- case-1/tracelog total_lines=600\n1\toriginal\n"
        )
        assert result.content.endswith("500\toriginal\n--- next_offset=500 eof=false")
    else:
        assert "original" not in result.content
