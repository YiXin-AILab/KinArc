"""Run in Tau2's environment against real Runtime and official Banking tools."""

import json
import os
import sys
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch

from benchmarks.tau2bench.lifecycle import current_attempt
from benchmarks.tau2bench.runtime import CaseRuntime
from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import ToolCall, UserMessage
from tau2.data_model.simulation import Results, TextRunConfig
from tau2.data_model.tasks import EvaluationCriteria, Task, UserScenario
from tau2.domains.banking_knowledge.data_model import Document, KnowledgeBase
from tau2.domains.banking_knowledge.environment import get_db, get_knowledge_base
from tau2.domains.banking_knowledge.retrieval import golden_prompt
from tau2.registry import registry
from tau2.runner import run_domain
from tau2.user.user_simulator_base import STOP, HalfDuplexUser

barrier = Barrier(2)
user_id = next(iter(get_db().users.data))
resources: list[tuple[str, str, Path]] = []
mode = os.environ.get("NATIVE_TEST_MODE", "normal")
runtimes: list[tuple[str, CaseRuntime]] = []
failed_once = False
golden = mode in {"golden", "golden_retry"}


class ObservedRuntime(CaseRuntime):
    """Observe actual owned resources without replacing their lifecycle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_cleanup = (
            mode == "cleanup_retry"
            and current_attempt().task_id == "case-a"
            and not failed_once
        )
        runtimes.append((current_attempt().simulation_id, self))
        if golden:
            assert self.agent_id == "banking-golden-agent"
            assert Path(self.environ["TAU2_DOMAIN_POLICY_FILE"]).is_file()

    def close(self) -> None:
        """Inject one attempt's persistent close error after releasing resources."""
        super().close()
        if self.fail_cleanup:
            raise RuntimeError("injected case-local cleanup failure")


# Native dependency types are exercised in the separate Tau2 environment.
class ScriptedUser(HalfDuplexUser[int]):  # type: ignore[misc]
    """Use real user tools, then inspect the same toolkit's business state."""

    def __init__(self, tools: list[Any], instructions: str, **kwargs: Any) -> None:
        super().__init__(tools=tools, instructions=instructions)
        self.label = "case-a" if "case-a" in instructions else "case-b"
        self.toolkit = next(
            t for t in tools if t.name == "submit_referral"
        )._func.__self__

    def get_init_state(self, message_history: Any = None) -> int:
        return 0

    def set_seed(self, seed: int) -> None:
        return

    def generate_next_message(
        self, message: Any, state: int
    ) -> tuple[UserMessage, int]:
        if state == 0:
            global failed_once
            if (
                mode in {"retry", "golden_retry", "cleanup_retry"}
                and self.label == "case-a"
                and not failed_once
            ):
                failed_once = True
                raise ValueError("injected initialization failure")
            context = current_attempt()
            resources.append((context.simulation_id, self.label, context.batch.source))
            return UserMessage(
                role="user",
                cost=0.0,
                tool_calls=[
                    ToolCall(
                        id=f"user-{self.label}",
                        name="submit_referral",
                        requestor="user",
                        arguments={"user_id": user_id, "account_type": self.label},
                    )
                ],
            ), 1
        if state == 1:
            assert "successfully" in message.content
            barrier.wait(timeout=30)
            # Both cases have written the same table before either agent reads it.
            content = f"native-case:{self.label} user:{user_id}"
            return UserMessage(role="user", content=content, cost=0.0), 2
        assert message.content == "done"
        rows = self.toolkit.query_database("users", json.dumps({"user_id": user_id}))
        assert f"{self.label}@example.invalid" in rows
        other = "case-b" if self.label == "case-a" else "case-a"
        assert f"{other}@example.invalid" not in rows
        referrals = self.toolkit.query_database(
            "referrals", json.dumps({"referrer_id": user_id})
        )
        assert self.label in referrals and other not in referrals
        return UserMessage(role="user", content=STOP, cost=0.0), 3


def tasks(task_split_name: str | None = None) -> list[Task]:
    """Small synthetic tasks use official domain data and real tool implementations."""
    return [
        Task(
            id=label,
            required_documents=[label] if golden else [],
            user_scenario=UserScenario(instructions=label),
            evaluation_criteria=EvaluationCriteria(),
        )
        for label in ("case-a", "case-b")
    ]


def golden_knowledge() -> KnowledgeBase:
    """Use official golden policy assembly with two distinguishable case documents."""
    return KnowledgeBase(
        documents={
            label: Document(id=label, title=label, content=f"golden-document:{label}")
            for label in ("case-a", "case-b")
        }
    )


def report_official_prompts() -> None:
    """Provide independent official model inputs for end-to-end comparison."""
    if golden or mode == "cli":
        filename = "required_docs.md" if golden else "classic_rag_bm25.md"
        template = Path("data/tau2/domains/banking_knowledge/prompts") / filename
        expected = {
            task.id: LLMAgent(
                tools=[],
                domain_policy=golden_prompt(template, golden_knowledge(), task),
                llm="openai/unused",
            ).system_prompt
            for task in tasks()
        }
        print("EXPECTED_SYSTEM_PROMPTS=" + json.dumps(expected), flush=True)


def main() -> None:
    """Exercise native scheduling, scoring and official output serialization."""
    report_official_prompts()
    registry.register_user(ScriptedUser, "native_scripted_user")
    if mode == "normal":
        import litellm

        litellm.model_cost["native-test"] = {
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.02,
            "litellm_provider": "openai",
            "mode": "chat",
        }
    registry.register_tasks(tasks, "native_scripted_tasks")
    output = Path(os.environ["NATIVE_TEST_OUTPUT"])
    config = TextRunConfig(
        domain="banking_knowledge",
        retrieval_config="golden_retrieval" if golden else "bm25_grep",
        agent="agentsystem_banking",
        llm_agent="openai/native-test",
        user="native_scripted_user",
        llm_user="openai/unused",
        task_set_name="native_scripted_tasks",
        max_concurrency=2,
        num_trials=2 if mode == "trials" else 1,
        max_retries=1 if mode in {"retry", "golden_retry", "cleanup_retry"} else 0,
        retry_delay=0,
        timeout=90,
        max_steps=4 if mode == "cutoff" else 30,
        save_to=str(output),
    )
    with (
        patch("benchmarks.tau2bench.adapter.CaseRuntime", ObservedRuntime),
        patch(
            "tau2.domains.banking_knowledge.environment.get_knowledge_base",
            side_effect=golden_knowledge if golden else get_knowledge_base,
        ),
    ):
        if mode == "cli" or golden:
            from tau2.cli import main as cli_main

            with patch.object(
                sys,
                "argv",
                [
                    "tau2",
                    "run",
                    "--domain",
                    "banking_knowledge",
                    "--agent",
                    "agentsystem_banking",
                    "--agent-llm",
                    "openai/native-test",
                    "--user",
                    "native_scripted_user",
                    "--user-llm",
                    "openai/unused",
                    "--task-set-name",
                    "native_scripted_tasks",
                    "--retrieval-config",
                    "golden_retrieval" if golden else "bm25_grep",
                    "--max-concurrency",
                    "2",
                    "--num-trials",
                    "1",
                    "--max-retries",
                    "1" if mode == "golden_retry" else "0",
                    "--timeout",
                    "90",
                    "--save-to",
                    str(output),
                ],
            ):
                cli_main()
            result = Results.model_validate_json((output / "results.json").read_text())
        else:
            result = run_domain(config)
    expected = 4 if mode == "trials" else 2
    assert len(result.simulations) == expected
    assert len({row[0] for row in resources}) == expected
    attempts = expected + (
        1 if mode in {"retry", "golden_retry", "cleanup_retry"} else 0
    )
    assert len(runtimes) == attempts
    assert len({identity for identity, _ in runtimes}) == attempts
    for field in ("root", "prefix", "session_id"):
        assert len({getattr(runtime, field) for _, runtime in runtimes}) == attempts
    for _, runtime in runtimes:
        assert not Path(runtime.environ["TAU2_DOMAIN_POLICY_FILE"]).exists()
        assert runtime.process is None
        assert not runtime.root.exists()
        assert not runtime.bridge.socket_path.exists()
    for simulation in result.simulations:
        assert simulation.user_cost == 0.0
        if mode != "cutoff":
            usages = [
                m.usage
                for m in simulation.messages
                if m.role == "assistant" and m.usage is not None
            ]
            assert sum(u["prompt_tokens"] for u in usages) == 30
            assert sum(u["completion_tokens"] for u in usages) == 15
            if mode == "normal":
                assert abs(simulation.agent_cost - 0.6) < 1e-9
            else:
                assert simulation.agent_cost is None
        else:
            assert simulation.agent_cost is None
        assert simulation.termination_reason.value == (
            "max_steps" if mode == "cutoff" else "user_stop"
        ), simulation.termination_reason
        assert simulation.reward_info is not None
        calls = [
            call
            for msg in simulation.messages
            for call in getattr(msg, "tool_calls", None) or []
        ]
        names = {call.name for call in calls}
        if mode == "cutoff":
            assert "change_user_email" not in names
            continue
        assert names >= {
            "submit_referral",
            "get_referrals_by_user",
            "change_user_email",
        }
        for call in calls:
            call_index = next(
                i
                for i, m in enumerate(simulation.messages)
                if call in (getattr(m, "tool_calls", None) or [])
            )
            result_index = next(
                i
                for i, m in enumerate(simulation.messages)
                if m.role == "tool" and m.id == call.id
            )
            assert call_index < result_index
    assert (output / "results.json").is_file()
    assert not (output / "agentsystem").exists()
    assert all(not path.exists() for _, _, path in resources)
    print("OWNED_PREFIXES=" + json.dumps([r.prefix for _, r in runtimes]))
    print("NATIVE_CASE_ISOLATION_OK")


if __name__ == "__main__":
    main()
