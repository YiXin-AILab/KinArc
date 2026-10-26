"""评测系统的严格事实模型：数据集、执行产出、评分与报告。"""

from datetime import UTC, datetime

from agentsystem.common.models import JsonValue, StrictModel
from pydantic import Field


class Expected(StrictModel):
    """单条 Case 的可机判期望；全部字段缺省，整体可以为 None。"""

    key_points: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    must_refuse: bool = False
    gold_doc_ids: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    max_latency_seconds: float | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)


class EvalCase(StrictModel):
    """数据集中的一条评测用例。

    rules 为 None 时按 expected 非空字段自动启用对应 L1 规则；
    dims 为 None 时启用全部适用的 judge 维度。
    """

    id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    expected: Expected | None = None
    rules: list[str] | None = None
    dims: list[str] | None = None
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, JsonValue] = Field(default_factory=dict)


class ToolCallRecord(StrictModel):
    """一次工具调用的公开事实。"""

    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class CaseOutcome(StrictModel):
    """Executor 对单条 Case 的执行产出。"""

    case_id: str = Field(min_length=1)
    reply: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    retrieved_doc_ids: list[str] = Field(default_factory=list)
    tool_outputs: list[str] = Field(default_factory=list)
    latency_seconds: float = Field(default=0.0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    error: str | None = None


class RuleResult(StrictModel):
    """一条 L1 规则的判定结果。"""

    rule: str = Field(min_length=1)
    passed: bool
    detail: str = ""


class DimScore(StrictModel):
    """单个 judge 维度的评分；score 为 None 表示评分失败降级。"""

    dimension: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0, le=1)
    weight: float = Field(default=1.0, gt=0)
    rationale: str = ""
    error: str | None = None


class CaseScore(StrictModel):
    """单条 Case 的规则与维度聚合评分。

    任一规则失败（或执行出错）时 failed=True 且 final_score=0；
    否则 final_score 为有分维度的加权平均，无可用维度时为 1.0。
    """

    case_id: str = Field(min_length=1)
    rules: list[RuleResult] = Field(default_factory=list)
    dims: list[DimScore] = Field(default_factory=list)
    final_score: float = Field(ge=0, le=1)
    failed: bool = False


class EvalReport(StrictModel):
    """一次评测 Run 的落盘事实模型（report.json）。"""

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    case_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    dim_averages: dict[str, float] = Field(default_factory=dict)
    final_score: float = Field(ge=0, le=1)
    cases: list[CaseScore] = Field(default_factory=list)


class JudgeLlmConfig(StrictModel):
    """Judge 裁判模型的连接配置；api_key 只允许经环境变量注入。"""

    model: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    timeout_seconds: float = Field(default=60.0, gt=0)


class EvalTaskConfig(StrictModel):
    """eval_tasks/<task_id>/config.yaml 的严格校验模型。"""

    agent_id: str = Field(min_length=1)
    server_base_url: str = "http://127.0.0.1:10061"
    dataset: str = Field(min_length=1)
    judge_llm: JudgeLlmConfig
    max_case_concurrency: int = Field(default=2, ge=1)
    judge_concurrency: int = Field(default=4, ge=1)


__all__ = [
    "CaseOutcome",
    "CaseScore",
    "DimScore",
    "EvalCase",
    "EvalReport",
    "EvalTaskConfig",
    "Expected",
    "JudgeLlmConfig",
    "RuleResult",
    "ToolCallRecord",
]
