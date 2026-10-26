"""L2 多维 LLM Judge：维度注册表、rubric Prompt、有界并发与失败降级。"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import litellm
from agentsystem.common.logging import log_unexpected_exception

from evaluation.models import CaseOutcome, DimScore, EvalCase, JudgeLlmConfig

_LOGGER = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """一个 judge 维度：默认权重、适用性判断与 rubric Prompt 构造。"""

    name: str
    weight: float
    build_prompt: Callable[[EvalCase, CaseOutcome], str]
    applicable: Callable[[EvalCase], bool]


def _always_applicable(case: EvalCase) -> bool:
    del case
    return True


def _requires_key_points(case: EvalCase) -> bool:
    return bool(case.expected and case.expected.key_points)


_CONTEXT_TOTAL_MAX_CHARS = 6000


def _format_reference_context(outcome: CaseOutcome) -> str:
    """把工具结果正文整理为 judge 的参考资料段落；无工具调用时明确标注。"""
    if not outcome.tool_outputs:
        return "参考资料：\n（无：本轮对话未调用任何检索或业务工具）"
    joined = "\n---\n".join(outcome.tool_outputs)
    header = "参考资料（助手工具调用返回的原文，是判断事实依据的基准）："
    return f"{header}\n{joined[:_CONTEXT_TOTAL_MAX_CHARS]}"


def _prompt_faithfulness(case: EvalCase, outcome: CaseOutcome) -> str:
    return f"""你是严格的评测裁判。请评估客服回复的忠实度（无幻觉）：
- 回复中的事实性陈述（价格、政策、流程、承诺）必须与「参考资料」一致或是其合理概括；
- 编造资料中不存在的价格、政策或承诺，或回复自相矛盾，应扣分；
- 不确定的内容应表明不确定性而不是编造；
- 若没有参考资料，仅判断回复是否自相矛盾或明显违背常识，不要仅因"未给出依据"扣分。

用户问题：{case.input}
助手回复：{outcome.reply or "（无回复）"}
{_format_reference_context(outcome)}

只输出 JSON，不要输出其他内容：
{{"score": 0到1之间的小数, "rationale": "一句话评分理由"}}"""


def _prompt_accuracy(case: EvalCase, outcome: CaseOutcome) -> str:
    key_points = case.expected.key_points if case.expected else []
    points_text = "\n".join(f"- {point}" for point in key_points)
    return f"""你是严格的评测裁判。请评估回复是否准确覆盖了以下关键要点。
逐条检查每个要点是否在回复中被正确表达（允许同义改写，不允许编造）。

用户问题：{case.input}
助手回复：{outcome.reply or "（无回复）"}
关键要点：
{points_text}

只输出 JSON，不要输出其他内容：
{{"score": 0到1之间的小数（按要点覆盖比例）, "rationale": "一句话评分理由"}}"""


def _prompt_completeness(case: EvalCase, outcome: CaseOutcome) -> str:
    return f"""你是严格的评测裁判。请评估回复的完整性：
是否完整回答了用户问题的所有方面，是否有明确的下一步指引，
是否存在明显遗漏或答非所问。

用户问题：{case.input}
助手回复：{outcome.reply or "（无回复）"}

只输出 JSON，不要输出其他内容：
{{"score": 0到1之间的小数, "rationale": "一句话评分理由"}}"""


def _prompt_compliance(case: EvalCase, outcome: CaseOutcome) -> str:
    return f"""你是严格的评测裁判。请评估回复的合规性：
语气是否专业礼貌；是否做出不当承诺（如绝对免费、百分百保证）；
是否泄露隐私或越权操作；是否遵守客服边界。

用户问题：{case.input}
助手回复：{outcome.reply or "（无回复）"}

只输出 JSON，不要输出其他内容：
{{"score": 0到1之间的小数, "rationale": "一句话评分理由"}}"""


def _prompt_human_likeness(case: EvalCase, outcome: CaseOutcome) -> str:
    return f"""你是严格的评测裁判。请评估客服回复的拟人度（像不像真人顾问）：
- 用户带情绪（着急、不满、抱怨）时，是否先回应情绪再进入事务；
- 语言是否自然口语化、以短句为主；
  出现公文腔（如「受理」「敬请谅解」「为您带来不便」）应扣分；
- 事实是否经过转述而不是机械照搬检索原文；
- 结尾是否有设计感（总结已办事项 + 明确下一步），
  机械套用「请问还有什么可以帮您」应扣分；
- 共情不应越权：为显得热情而承诺赔偿、免费或确定结果，应扣分。

用户问题：{case.input}
助手回复：{outcome.reply or "（无回复）"}

只输出 JSON，不要输出其他内容：
{{"score": 0到1之间的小数, "rationale": "一句话评分理由"}}"""


DIMENSION_REGISTRY: dict[str, DimensionSpec] = {
    spec.name: spec
    for spec in (
        DimensionSpec(
            name="faithfulness",
            weight=1.0,
            build_prompt=_prompt_faithfulness,
            applicable=_always_applicable,
        ),
        DimensionSpec(
            name="accuracy",
            weight=1.0,
            build_prompt=_prompt_accuracy,
            applicable=_requires_key_points,
        ),
        DimensionSpec(
            name="completeness",
            weight=1.0,
            build_prompt=_prompt_completeness,
            applicable=_always_applicable,
        ),
        DimensionSpec(
            name="compliance",
            weight=1.0,
            build_prompt=_prompt_compliance,
            applicable=_always_applicable,
        ),
        DimensionSpec(
            name="human_likeness",
            weight=1.0,
            build_prompt=_prompt_human_likeness,
            applicable=_always_applicable,
        ),
    )
}


def select_dimensions(case: EvalCase) -> list[DimensionSpec]:
    """解析用例启用的裁判维度，未显式配置时选择全部适用维度。

    Args:
        case: 待评分用例，dims 决定显式启用的维度。

    Returns:
        按配置顺序排列的维度定义；未配置时使用注册顺序并检查适用性。

    Raises:
        ValueError: 显式配置中包含未注册的维度。
    """
    if case.dims is None:
        return [spec for spec in DIMENSION_REGISTRY.values() if spec.applicable(case)]
    unknown = [name for name in case.dims if name not in DIMENSION_REGISTRY]
    if unknown:
        raise ValueError(f"未知 judge 维度：{', '.join(sorted(unknown))}")
    return [DIMENSION_REGISTRY[name] for name in case.dims]


def extract_json_object(text: str) -> dict[str, object]:
    """截取裁判输出中首个左花括号至最后一个右花括号之间的 JSON。

    Args:
        text: 裁判返回的原始文本。

    Returns:
        解析后的 JSON 对象。

    Raises:
        ValueError: 未找到对象边界、JSON 无效或解析结果不是对象。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("裁判输出中不存在 JSON 对象")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("裁判输出不是 JSON 对象")
    return parsed


class LlmJudge:
    """基于 litellm 的多维裁判；评分失败重试一次后降级为 score=None。"""

    def __init__(self, config: JudgeLlmConfig, *, concurrency: int = 4) -> None:
        """保存裁判配置，并限制同时进行的维度评分调用数。

        Args:
            config: 裁判模型的连接和超时配置。
            concurrency: 同时进行的模型调用数量上限。

        Raises:
            ValueError: 并发上限小于 1。
        """
        if concurrency < 1:
            raise ValueError("judge 并发必须为正整数")
        self._config = config
        self._semaphore = asyncio.Semaphore(concurrency)

    async def score_cases(
        self,
        items: list[tuple[EvalCase, CaseOutcome]],
    ) -> list[list[DimScore]]:
        """并发评分多条用例，模型调用受共享并发上限约束。

        Args:
            items: 按顺序排列的用例及其执行事实。

        Returns:
            与输入顺序一致的维度评分列表。
        """
        return await asyncio.gather(
            *(self.score_case(case, outcome) for case, outcome in items)
        )

    async def score_case(
        self,
        case: EvalCase,
        outcome: CaseOutcome,
    ) -> list[DimScore]:
        """对单条用例的启用维度评分，执行失败的用例直接跳过。

        Args:
            case: 决定启用维度及参考答案的评测用例。
            outcome: 用例的执行事实。

        Returns:
            维度评分列表；执行失败时为空，裁判重试后仍失败的维度 score 为 None。

        Raises:
            ValueError: 用例显式指定了未注册的维度。
        """
        if outcome.error is not None:
            return []
        specs = select_dimensions(case)
        return await asyncio.gather(
            *(self._score_dimension(spec, case, outcome) for spec in specs)
        )

    async def _score_dimension(
        self,
        spec: DimensionSpec,
        case: EvalCase,
        outcome: CaseOutcome,
    ) -> DimScore:
        last_error = "未知错误"
        for _ in range(_MAX_ATTEMPTS):
            async with self._semaphore:
                try:
                    return await asyncio.wait_for(
                        self._request_score(spec, case, outcome),
                        timeout=self._config.timeout_seconds,
                    )
                except Exception as error:  # noqa: BLE001 - 失败必须降级为 error 字段
                    last_error = type(error).__name__
                    log_unexpected_exception(
                        _LOGGER,
                        "judge_dimension_failed",
                        error,
                        operation="evaluation.judge",
                        dimension=spec.name,
                        case_id=case.id,
                    )
        return DimScore(
            dimension=spec.name,
            score=None,
            weight=spec.weight,
            error=f"裁判评分失败（{last_error}）",
        )

    async def _request_score(
        self,
        spec: DimensionSpec,
        case: EvalCase,
        outcome: CaseOutcome,
    ) -> DimScore:
        # api_key 只进入 litellm 调用参数，不写入任何日志或评分结果。
        response = await litellm.acompletion(
            model=self._config.model,
            api_key=self._config.api_key,
            base_url=self._config.base_url,
            messages=[{"role": "user", "content": spec.build_prompt(case, outcome)}],
        )
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("裁判输出为空")
        parsed = extract_json_object(content)
        score = parsed.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("裁判输出缺少数值 score")
        if not 0.0 <= float(score) <= 1.0:
            raise ValueError("裁判 score 超出 0-1 范围")
        rationale = parsed.get("rationale")
        return DimScore(
            dimension=spec.name,
            score=float(score),
            weight=spec.weight,
            rationale=str(rationale) if rationale is not None else "",
        )


__all__ = [
    "DIMENSION_REGISTRY",
    "DimensionSpec",
    "LlmJudge",
    "extract_json_object",
    "select_dimensions",
]
