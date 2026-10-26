"""L1 确定性规则库：注册表、内置规则与按 expected 的自动启用逻辑。"""

import re
from collections.abc import Callable

from evaluation.models import CaseOutcome, Expected, RuleResult

type RuleFn = Callable[[CaseOutcome, Expected], RuleResult]

RULE_REGISTRY: dict[str, RuleFn] = {}

# 常见中文拒答句式；可通过 is_refusal 的 keywords 参数覆盖。
REFUSAL_KEYWORDS: tuple[str, ...] = (
    "无法回答",
    "无法提供",
    "不能回答",
    "不能给您",
    "无法为您",
    "不方便透露",
    "不便提供",
    "超出了我",
    "不在我的专业范围",
    "不在我的服务范围",
    "不在服务范围",
    "帮不上忙",
    "帮不上您",
    "我的知识库中没有",
    "没有相关信息",
    "没有相关资料",
    "建议您咨询",
    "无法确认",
)


def register_rule(name: str) -> Callable[[RuleFn], RuleFn]:
    """返回规则注册装饰器，名称重复时拒绝注册，不覆盖已有规则。"""

    def decorator(fn: RuleFn) -> RuleFn:
        if name in RULE_REGISTRY:
            raise ValueError(f"规则重复注册：{name}")
        RULE_REGISTRY[name] = fn
        return fn

    return decorator


def is_refusal(reply: str, *, keywords: tuple[str, ...] = REFUSAL_KEYWORDS) -> bool:
    """用关键词启发式判断回复是否构成拒答。

    Args:
        reply: 待判断的回复正文。
        keywords: 用于识别拒答的关键词。

    Returns:
        是否包含任一拒答关键词。
    """
    return any(keyword in reply for keyword in keywords)


def auto_rules(expected: Expected) -> list[str]:
    """按 expected 中非空/非默认字段自动启用对应规则。"""
    enabled: list[str] = []
    if expected.gold_doc_ids:
        enabled.append("retrieval_hit")
    if expected.forbidden:
        enabled.append("forbidden_words")
    if expected.must_refuse:
        enabled.append("refusal_correct")
    if expected.required_tools:
        enabled.append("required_tools_called")
    if expected.forbidden_tools:
        enabled.append("forbidden_tools_not_called")
    if expected.max_latency_seconds is not None:
        enabled.append("latency_under")
    if expected.max_total_tokens is not None:
        enabled.append("tokens_under")
    return enabled


def select_rules(case_rules: list[str] | None, expected: Expected | None) -> list[str]:
    """解析 Case 最终启用的规则名列表并校验存在性。

    Args:
        case_rules: 显式规则名称；为 None 时根据期望结果自动选择。
        expected: 用例的期望结果与规则约束。

    Returns:
        按配置顺序排列的已注册规则名称。
    """
    names = (
        case_rules
        if case_rules is not None
        else (auto_rules(expected) if expected is not None else [])
    )
    unknown = [name for name in names if name not in RULE_REGISTRY]
    if unknown:
        raise ValueError(f"未知规则：{', '.join(sorted(unknown))}")
    return names


def run_rules(
    names: list[str],
    outcome: CaseOutcome,
    expected: Expected,
) -> list[RuleResult]:
    """按顺序执行规则；执行出错的 Case 所有规则直接失败。

    Args:
        names: 按执行顺序排列的已注册规则名称。
        outcome: 用例的执行事实。
        expected: 用例的期望结果与规则约束。

    Returns:
        与输入规则顺序一致的判定结果。
    """
    if outcome.error is not None:
        return [
            RuleResult(
                rule=name,
                passed=False,
                detail=f"执行失败：{outcome.error}",
            )
            for name in names
        ]
    return [RULE_REGISTRY[name](outcome, expected) for name in names]


@register_rule("retrieval_hit")
def _retrieval_hit(outcome: CaseOutcome, expected: Expected) -> RuleResult:
    missing = [
        doc_id
        for doc_id in expected.gold_doc_ids
        if doc_id not in outcome.retrieved_doc_ids
    ]
    return RuleResult(
        rule="retrieval_hit",
        passed=not missing,
        detail=(
            "全部命中" if not missing else f"未检索到 gold 文档：{', '.join(missing)}"
        ),
    )


@register_rule("forbidden_words")
def _forbidden_words(outcome: CaseOutcome, expected: Expected) -> RuleResult:
    hits = [
        word
        for word in expected.forbidden
        if _asserted_occurrences(outcome.reply, word)
    ]
    return RuleResult(
        rule="forbidden_words",
        passed=not hits,
        detail="未出现违禁词" if not hits else f"回复包含违禁词：{', '.join(hits)}",
    )


# 违禁词前近距离出现否定词时视为“引用并澄清误解”，不算违规表述。
_NEGATION_MARKERS: tuple[str, ...] = ("不", "没", "并非")
_NEGATION_WINDOW = 6


def _asserted_occurrences(text: str, word: str) -> bool:
    """判断违禁词是否以肯定陈述方式出现（紧邻否定词的澄清性引用除外）。"""
    start = 0
    while True:
        index = text.find(word, start)
        if index < 0:
            return False
        prefix = text[max(0, index - _NEGATION_WINDOW) : index]
        if not any(marker in prefix for marker in _NEGATION_MARKERS):
            return True
        start = index + 1


@register_rule("refusal_correct")
def _refusal_correct(outcome: CaseOutcome, expected: Expected) -> RuleResult:
    refused = is_refusal(outcome.reply)
    passed = refused == expected.must_refuse
    if expected.must_refuse:
        detail = "正确拒答" if passed else "期望拒答但回复未拒答"
    else:
        detail = "未误拒答" if passed else "不期望拒答但回复构成拒答"
    return RuleResult(rule="refusal_correct", passed=passed, detail=detail)


@register_rule("required_tools_called")
def _required_tools_called(outcome: CaseOutcome, expected: Expected) -> RuleResult:
    called = {record.name for record in outcome.tool_calls}
    missing = [name for name in expected.required_tools if name not in called]
    return RuleResult(
        rule="required_tools_called",
        passed=not missing,
        detail=(
            "必需工具均已调用" if not missing else f"缺少工具调用：{', '.join(missing)}"
        ),
    )


@register_rule("forbidden_tools_not_called")
def _forbidden_tools_not_called(outcome: CaseOutcome, expected: Expected) -> RuleResult:
    called = {record.name for record in outcome.tool_calls}
    hits = [name for name in expected.forbidden_tools if name in called]
    return RuleResult(
        rule="forbidden_tools_not_called",
        passed=not hits,
        detail=("未调用违禁工具" if not hits else f"调用了违禁工具：{', '.join(hits)}"),
    )


@register_rule("latency_under")
def _latency_under(outcome: CaseOutcome, expected: Expected) -> RuleResult:
    assert expected.max_latency_seconds is not None
    passed = outcome.latency_seconds <= expected.max_latency_seconds
    return RuleResult(
        rule="latency_under",
        passed=passed,
        detail=(
            f"延迟 {outcome.latency_seconds:.2f}s"
            f"（上限 {expected.max_latency_seconds:.2f}s）"
        ),
    )


@register_rule("tokens_under")
def _tokens_under(outcome: CaseOutcome, expected: Expected) -> RuleResult:
    assert expected.max_total_tokens is not None
    if outcome.total_tokens is None:
        return RuleResult(
            rule="tokens_under",
            passed=False,
            detail="API 未透出 token 用量，无法判定",
        )
    passed = outcome.total_tokens <= expected.max_total_tokens
    return RuleResult(
        rule="tokens_under",
        passed=passed,
        detail=(f"tokens {outcome.total_tokens}（上限 {expected.max_total_tokens}）"),
    )


_DOC_ID_PATTERN = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def parse_retrieved_doc_ids(text: str) -> list[str]:
    """从 kb_search 工具结果正文解析检索命中的 doc_id（保持出现顺序去重）。"""
    seen: set[str] = set()
    doc_ids: list[str] = []
    for match in _DOC_ID_PATTERN.finditer(text):
        doc_id = match.group(1)
        if doc_id not in seen:
            seen.add(doc_id)
            doc_ids.append(doc_id)
    return doc_ids


__all__ = [
    "REFUSAL_KEYWORDS",
    "RULE_REGISTRY",
    "auto_rules",
    "is_refusal",
    "parse_retrieved_doc_ids",
    "register_rule",
    "run_rules",
    "select_rules",
]
