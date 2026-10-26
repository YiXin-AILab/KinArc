"""通过现有 Runtime 服务边界执行一次候选试验。"""

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentsystem.common.models import FinishReason, SessionContext
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import AgentConfig, ModelParamsConfig, RuntimeConfig
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.provider import LLMProvider
from agentsystem.message.models import TextBlock
from agentsystem.plugin.models import PluginSource
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import SessionSnapshot, SubmitMessageCommand
from agentsystem.tool.models import ToolDefinition
from application.evoagentbench_knowledge_agent.plugins.docker import DockerEnvironment
from application.evoagentbench_knowledge_agent.plugins.execution import (
    ExecutionEnvironment,
    execution_tools,
)
from application.evoagentbench_knowledge_agent.plugins.office_images import OfficeImages
from application.evoagentbench_research_agent.plugins.sse import sse_tools
from evolution.models import CaseAttempt, ExecutionMetadata

from benchmarks.evoagentbench.domains.retrieval_finalizer import Finalization, finalize
from benchmarks.evoagentbench.runners.skills import (
    CandidateSkillAuthorization,
    configure_skills,
    skill_roots,
)
from benchmarks.evoagentbench.runners.tools import state_tools


def asset(root: Path, relative: str) -> Path:
    """拒绝逃逸冻结候选目录的资产路径。"""
    path = (root / relative).resolve(strict=True)
    path.relative_to(root.resolve())
    return path


async def run(
    target: Path,
    runtime_path: Path,
    prompt: str,
    timeout: float,
    scratch: Path,
    *,
    llm_provider: LLMProvider | None = None,
    execution_image: str | None = None,
    container_name: str | None = None,
    domain_environment: dict[str, str] | None = None,
    image_archive: Path | None = None,
    candidate_plugins_enabled: bool = False,
    model_contract: dict[str, Any] | None = None,
) -> CaseAttempt:
    """在 Runtime 生命周期之外管理可选任务环境。

    Args:
        target: 包含配置、提示词和学习资产的冻结候选根目录。
        runtime_path: 当前隔离试验使用的 Runtime YAML 文件。
        prompt: 官方领域 adapter 提供的公开任务提示词。
        timeout: Runtime 执行超时秒数，由内部 worker 强制执行。
        scratch: 当前试验的临时记忆与 Runtime 数据目录。
        llm_provider: 为确定性测试注入的可选模型提供器。
        execution_image: 创建自有通用任务容器时使用的可选镜像。
        container_name: 通用任务容器的可选名称。
        domain_environment: 描述上游持有任务环境的公开字段。
        image_archive: 持久化办公图片观察证据的可选目录。

    Returns:
        包含最终输出、公开执行轨迹和用量的单次执行记录。

    Raises:
        ValueError: 领域环境与通用环境冲突，或配置无效。
        TimeoutError: Runtime 执行超过试验时限。
    """
    environment = (
        DockerEnvironment(execution_image, name=container_name)
        if execution_image
        else None
    )
    try:
        if environment:
            await environment.start()
        domain_tools: tuple[ToolDefinition, ...] = ()
        bound: ExecutionEnvironment | None = environment
        workspace = "/workspace"
        images: OfficeImages | None = None
        if domain_environment:
            from benchmarks.evoagentbench.domains.domain_environment import (
                AttachedContainer,
            )

            if environment:
                raise ValueError("Domain and generic environments are exclusive")
            kind = domain_environment["kind"]
            if kind == "research":
                domain_tools = await sse_tools(domain_environment["url"])
            elif kind == "knowledge":
                bound = AttachedContainer(domain_environment["container"])
                workspace = domain_environment["workspace"]
                images = OfficeImages(bound, workspace, evidence_dir=image_archive)
            else:
                raise ValueError("Unknown domain environment")
        return await _run(
            target,
            runtime_path,
            prompt,
            timeout,
            scratch,
            llm_provider=llm_provider,
            environment=bound,
            domain_tools=domain_tools,
            workspace=workspace,
            images=images,
            candidate_plugins_enabled=candidate_plugins_enabled,
            model_contract=model_contract,
            finalize_empty_response=bool(
                domain_environment and domain_environment["kind"] == "research"
            ),
        )
    finally:
        if environment:
            await environment.close()


async def _run(
    target: Path,
    runtime_path: Path,
    prompt: str,
    timeout: float,
    scratch: Path,
    *,
    llm_provider: LLMProvider | None,
    environment: ExecutionEnvironment | None,
    domain_tools: tuple[ToolDefinition, ...] = (),
    workspace: str = "/workspace",
    images: OfficeImages | None = None,
    finalize_empty_response: bool = False,
    candidate_plugins_enabled: bool = False,
    model_contract: dict[str, Any] | None = None,
) -> CaseAttempt:
    """为每次独立试验使用新的宿主、命名空间和会话。"""
    runtime = load_yaml_config(runtime_path, RuntimeConfig)
    if (
        runtime.managers.message.type != "redis"
        or runtime.managers.state.type != "redis"
    ):
        raise ValueError(
            "EvoAgentBench currently requires Redis message/state isolation"
        )
    identifier = (
        os.environ["EAB_RESEARCH_SESSION_ID"]
        if candidate_plugins_enabled
        else uuid4().hex
    )
    scratch.mkdir(parents=True, exist_ok=True)
    runtime = runtime.model_copy(
        update={
            "redis": runtime.redis.model_copy(
                update={"key_prefix": f"eab:{identifier}"}
            )
        }
    )
    config = load_yaml_config(target / "config/agent.yaml", AgentConfig)
    frozen_config_sha256 = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
    if images and config.enabled_middlewares is not None:
        raise ValueError("Office image tools require the default middleware profile")
    plugins: tuple[PluginSource, ...] = ()
    if candidate_plugins_enabled:
        from evolution_tasks.evoagentbench_research._internal import (
            research_candidate,
        )

        if not finalize_empty_response or model_contract is None:
            raise ValueError("Candidate code requires the isolated research contract")
        plugins = research_candidate.candidate_plugins(target, config, model_contract)
    tools = state_tools(target, scratch) + domain_tools
    if environment:
        tools += execution_tools(environment, workspace=workspace)
    if images:
        tools += (images.tool(),)
    if not candidate_plugins_enabled and tuple(
        name for name in (config.enabled_tools or ()) if name != "skill_view"
    ) != tuple(tool.name for tool in tools):
        raise ValueError("Enabled tools must match the frozen tool profile")
    if not candidate_plugins_enabled and config.enabled_context_providers:
        raise ValueError("Dynamic context providers are not enabled in this protocol")
    config = configure_skills(target, config)
    roots = skill_roots(target, config)
    authorization = CandidateSkillAuthorization(roots, config.enabled_skills)
    spec = AgentRuntimeSpec(
        agent_config=config,
        base_prompt=asset(target, config.prompt).read_text(encoding="utf-8"),
        skill_roots=roots,
        external_tools=tools,
        llm_provider=llm_provider,
        plugin_sources=plugins + ((images.plugin(),) if images else ()),
    )
    host = await ServiceHost.create(
        runtime_config=runtime,
        project_root=scratch,
        agents=(spec,),
        consumer_id=f"eab-{identifier}",
        tool_authorization=authorization,
    )
    started = time.monotonic()
    if images:
        images.upload = host.application.upload_asset
    try:
        await host.start()
        async with asyncio.timeout(timeout):
            trial = await execute(
                host,
                config.agent_id,
                identifier,
                prompt,
                started,
                finalization_model=config.llm_model
                if finalize_empty_response
                else None,
                finalization_provider=llm_provider,
            )
            assert isinstance(trial.tracelog, dict)
            return trial.model_copy(
                update={
                    "tracelog": {
                        **trial.tracelog,
                        "agent_config_sha256": frozen_config_sha256,
                        "effective_agent_config_sha256": hashlib.sha256(
                            config.model_dump_json().encode()
                        ).hexdigest(),
                        "enabled_skills": list(config.enabled_skills),
                        "image_evidence": images.evidence if images else [],
                    }
                }
            )
    finally:
        await host.close()


async def execute(
    host: ServiceHost,
    agent_id: str,
    identifier: str,
    prompt: str,
    started: float,
    *,
    finalization_model: ModelParamsConfig | None = None,
    finalization_provider: LLMProvider | None = None,
) -> CaseAttempt:
    """收集最终输出和执行轨迹，不将快照作为评分依据。

    由持有试验的 worker 提供整体时限；检索执行成功但正文为空时，
    允许在同一时限内发起一次独立的答案收尾请求。

    Args:
        host: 当前试验已启动的隔离 Runtime 服务宿主。
        agent_id: 已注册到服务宿主的 Agent 标识。
        identifier: 当前试验的用户、会话和输入消息标识。
        prompt: 作为用户输入提交的公开任务提示词。
        started: 用于计算完整试验耗时的单调时钟起始值。
        finalization_model: 对空答案进行收尾的可选模型参数。
        finalization_provider: 收尾请求使用的可选模型提供器覆盖值。

    Returns:
        最终输出与已观测用量，并显式标记缺失的用量统计。
    """
    context = SessionContext(
        app_id="evoagentbench",
        agent_id=agent_id,
        user_id=identifier,
        session_id=identifier,
    )
    await host.application.submit_message(
        context,
        SubmitMessageCommand(
            message_id=identifier,
            role=InboundRole.USER,
            origin="evoagentbench",
            content=(TextBlock(text=prompt),),
        ),
    )
    snapshot = await _terminal_snapshot(host, context)
    response = snapshot.result.content if snapshot.result else None
    history = await host.application.list_messages(context)
    finalization: Finalization | None = None
    if (
        finalization_model is not None
        and snapshot.status is SessionStatus.SUCCEEDED
        and not (response or "").strip()
    ):
        # 使用独立答案请求，避免再次注入 Skill 或检索要求。
        # 该请求归属当前试验，沿用已有外层超时，不执行评分。
        provider = finalization_provider or LiteLLMProvider(
            api_key=finalization_model.api_key,
            api_base=finalization_model.base_url,
            timeout_seconds=finalization_model.timeout_seconds,
        )
        finalization = await finalize(
            provider,
            finalization_model.to_generation_config(),
            prompt,
            history,
            context_window=finalization_model.context_window,
        )
        response = finalization.answer
    total_tokens = snapshot.usage.total_tokens if snapshot.usage else 0
    modes = [snapshot.usage.accounting.value if snapshot.usage else "missing"]
    if finalization:
        total_tokens += finalization.response.usage.total_tokens
        modes.append(finalization.response.usage.accounting.value)
    status = snapshot.status.value
    if finalization and finalization.response.finish_reason is FinishReason.ERROR:
        status = SessionStatus.FAILED.value
    accounting = (
        "missing"
        if "missing" in modes
        else "partial"
        if "partial" in modes
        else "total_only"
        if "total_only" in modes
        else "exact"
    )
    return CaseAttempt(
        result=response or "",
        tracelog={
            "status": status,
            "snapshot": snapshot.model_dump(mode="json"),
            "messages": history.model_dump(mode="json"),
            "token_accounting": accounting,
            "run_snapshots": [snapshot.model_dump(mode="json")],
            "finalization_followups": int(finalization is not None),
            "finalization": finalization.model_dump(mode="json")
            if finalization
            else None,
        },
        metadata=ExecutionMetadata(
            # 历史元数据记录已观测数量；报告使用 token_accounting
            # 区分未知成本与实际测得的零 token 执行。
            total_tokens=total_tokens,
            duration_seconds=time.monotonic() - started,
        ),
    )


async def _terminal_snapshot(
    host: ServiceHost,
    context: SessionContext,
) -> SessionSnapshot:
    """等待唯一一次 Runtime 执行结束；答案收尾不创建第二次会话执行。"""
    while True:
        snapshot = await host.application.snapshot(context)
        if snapshot is not None and snapshot.status in {
            SessionStatus.SUCCEEDED,
            SessionStatus.FAILED,
            SessionStatus.CANCELED,
        }:
            return snapshot
        await asyncio.sleep(0.05)


def main() -> None:
    """执行显式 worker 请求，同时由监督器设置硬超时。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    with tempfile.TemporaryDirectory(prefix="eab-scratch-") as directory:
        result = asyncio.run(
            run(
                Path(request["target"]),
                Path(request["runtime_config"]),
                request["prompt"],
                request["timeout"],
                Path(directory),
                execution_image=request.get("execution_image"),
                container_name=request.get("container_name"),
                domain_environment=request.get("domain_environment"),
                candidate_plugins_enabled=request.get(
                    "candidate_plugins_enabled", False
                ),
                model_contract=request.get("model_contract"),
                image_archive=Path(request["image_archive"])
                if request.get("image_archive")
                else None,
            )
        )
        args.result.write_text(result.model_dump_json(), encoding="utf-8")


if __name__ == "__main__":
    main()
