"""校验公开答案并提供基于证据的检索反馈，不引入标准答案。"""

from collections.abc import Sequence
from typing import Any


def no_answer_reason(response: str) -> str | None:
    """识别空输出及 Nanobot 明确表示未交付答案的标记。"""
    if not response.strip():
        return "no_public_answer"
    if "I've completed processing but have no response to give." in response:
        return "nanobot_no_response"
    if "I reached the maximum number of tool call iterations (" in response:
        return "nanobot_iteration_limit"
    return None


def summarize_retrieval_trials(
    trials: Sequence[tuple[str, dict[str, Any]]],
) -> tuple[str, str | None]:
    """报告观测到的失败，不推测事实错误，也不修改分数。"""
    notes: list[str] = []
    kinds: list[str] = []
    for answer, trace in trials:
        verdict = trace.get("verifier", {})
        method = verdict.get("method")
        if method == "user_requested_20min_failure":
            notes.append(
                "The full trial exceeded the user-requested 1200-second deadline "
                "and counts as zero; this is not an official judge verdict. "
                "Do not retry."
            )
            kinds.append("retrieval.user_deadline")
        elif no_answer_reason(answer):
            notes.append(
                "No public final answer was delivered; private reasoning is not "
                "a submission. The no-answer guard assigns zero. A single "
                "final-answer generation, when enabled, is recorded "
                "in the execution trace."
            )
            kinds.append("retrieval.no_answer")
        elif verdict.get("passed"):
            notes.append(f"The public answer passed verification ({method}).")
        else:
            notes.append(
                f"The submitted public answer did not pass verification ({method}). "
                "No independently verified factual error explanation is available; "
                "inspect the public question, response and tool evidence."
            )
            kinds.append("retrieval.answer_incorrect")
    return "\n".join(notes), kinds[0] if len(
        set(kinds)
    ) == 1 else "unclassified_failure" if kinds else None
