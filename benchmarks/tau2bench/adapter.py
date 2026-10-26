"""由 KinArc 的 agentsystem Runtime 驱动的 Tau2 HalfDuplexAgent。"""

import time
from collections.abc import Callable
from typing import Any

from tau2.agent.base_agent import AgentError, HalfDuplexAgent, ValidAgentInputMessage
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from tau2.environment.tool import Tool

from benchmarks.tau2bench.accounting import estimate_cost, token_usage
from benchmarks.tau2bench.lifecycle import current_attempt
from benchmarks.tau2bench.runtime import CaseRuntime
from benchmarks.tau2bench.session import HttpSession
from benchmarks.tau2bench.tool_bridge import ToolCall as BridgeCall
from benchmarks.tau2bench.tool_bridge import ToolMessage as BridgeResult


# Tau2 基类缺少类型信息，由其独立环境中的合约测试验证。
class AgentsystemBankingAgent(HalfDuplexAgent[None]):  # type: ignore[misc]
    """由 Tau2 执行业务工具，再让 Runtime 继续推理。"""

    def __init__(self, tools: list[Tool], domain_policy: str) -> None:
        """在当前官方 simulation 作用域内获取资源。"""
        super().__init__(tools=tools, domain_policy=domain_policy)
        context = current_attempt()
        batch = context.batch
        self._deadline = time.monotonic() + batch.timeout
        self.runtime = CaseRuntime(
            batch.integration,
            batch.source,
            model=batch.model,
            temperature=batch.temperature,
            cancel=context.cancel,
            log_path=context.log_path,
            domain_policy=domain_policy,
        )
        self.session: HttpSession | None = None
        context.own(self.stop)
        self.seed: int | None = None
        self._model = batch.model
        self._turn_messages: list[AssistantMessage] = []

    def get_init_state(self, message_history: list[Message] | None = None) -> None:
        """每次 simulation 从空状态开始；任务初始化由 Tau2 负责。"""
        from tau2.orchestrator.orchestrator import DEFAULT_FIRST_AGENT_MESSAGE

        if message_history and not (
            len(message_history) == 1
            and isinstance(message_history[0], AssistantMessage)
            and message_history[0].content == DEFAULT_FIRST_AGENT_MESSAGE.content
            and not message_history[0].tool_calls
        ):
            raise ValueError("preloaded agent message history is not supported")
        return None

    def set_seed(self, seed: int) -> None:
        """仅在实例内保存随机种子，不修改全局随机状态。"""
        self.seed = seed

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: None
    ) -> tuple[AssistantMessage, None]:
        """转发用户消息或官方工具结果，并返回下一步动作。"""
        try:
            session = self._session()
            if isinstance(message, UserMessage):
                session.start(message.content or "")
            elif isinstance(message, (ToolMessage, MultiToolMessage)):
                messages = (
                    message.tool_messages
                    if isinstance(message, MultiToolMessage)
                    else [message]
                )
                for result in messages:
                    self.runtime.bridge.deliver(
                        BridgeResult.model_validate(result.model_dump())
                    )
            else:
                raise ValueError("unsupported Tau2 input message")
            output = session.next_output(self.runtime.bridge)
            if isinstance(output, BridgeCall):
                response = AssistantMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(**output.model_dump()),
                    ],
                )
                self._turn_messages.append(response)
                return response, state
            response = AssistantMessage(role="assistant", content=output)
            if session.usage is not None:
                response.usage = token_usage(session.usage)
                if len(session.models) <= 1 and not session.compacted:
                    model = next(iter(session.models), self._model)
                    response.cost = estimate_cost(model, session.usage)
                # Tau2 会保留这些消息对象直到评测结束；整个 Runtime run 的用量
                # 只计入最终回复，工具交接时不重复计数。
                for message in self._turn_messages:
                    message.cost = 0.0
                    message.usage = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    }
            self._turn_messages.clear()
            return response, state
        except Exception as error:
            # 官方 Orchestrator 负责处理 AgentError，并保留对应轨迹。
            raise AgentError("agentsystem banking execution failed") from error

    def _session(self) -> HttpSession:
        if self.session is None:
            self.runtime.start()
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Runtime startup exhausted case timeout")
            self.session = HttpSession(
                self.runtime.base_url,
                self.runtime.session_id,
                agent_id=self.runtime.agent_id,
                timeout=remaining,
                http_timeout=self.runtime.config.http_timeout_seconds,
                cancel=self.runtime.cancel,
            )
        return self.session

    def stop(
        self, message: ValidAgentInputMessage | None = None, state: None = None
    ) -> None:
        """先停止 Runtime，再等待 SSE 线程；即使失败也逐项清理资源。"""
        closers: list[Callable[[], None]] = [self.runtime.close]
        session = self.session
        if session is not None:
            closers.insert(0, session.request_stop)
            closers.append(
                lambda: session.close(
                    timeout=self.runtime.config.shutdown_timeout_seconds
                )
            )
        errors: list[Exception] = []
        for close in closers:
            try:
                close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("native tau2 agent cleanup failed", errors)


def create_agent(
    tools: list[Tool], domain_policy: str, **kwargs: Any
) -> AgentsystemBankingAgent:
    """原生 Factory 入口；运行参数从当前尝试上下文获取。"""
    del kwargs
    return AgentsystemBankingAgent(tools, domain_policy)
