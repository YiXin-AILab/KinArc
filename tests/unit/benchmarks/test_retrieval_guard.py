"""空公开答案不能因评分模型幻觉而被判定为通过。"""

from pathlib import Path
from typing import Any

import pytest
from benchmarks.evoagentbench.domains import domain_worker


@pytest.mark.parametrize(
    "answer",
    [
        "",
        " \n",
        "Setup\nI've completed processing but have no response to give.",
        "Setup\nI reached the maximum number of tool call iterations (40)",
    ],
)
def test_no_answer_is_zero_before_judge(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Adapter:
        pass_threshold = staticmethod(lambda: 0.0)
        initialize = pre_task_trials = cleanup = post_task_trials = finalize = (
            staticmethod(lambda *args: None)
        )
        setup = staticmethod(lambda *args: {})
        build_prompt = staticmethod(lambda *args: "public question")

        def verify(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append("judge")
            return {"reward": 1, "method": "llm_judge"}

    monkeypatch.setattr(
        domain_worker,
        "run_runtime",
        lambda *args: {"result": answer, "tracelog": {"status": "succeeded"}},
    )
    result = domain_worker.trial(
        Adapter(),
        {"name": "1"},
        {
            "settings": {"domain": "information_retrieval", "runner": "runtime"},
            "job_tag": "unique",
        },
    )
    assert calls == []
    assert result["tracelog"]["verifier"]["score"] == 0
    assert result["tracelog"]["verifier"]["method"] == "no_answer_guard"


def test_nonempty_answer_still_uses_official_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        pass_threshold = staticmethod(lambda: 0.0)
        initialize = pre_task_trials = cleanup = post_task_trials = finalize = (
            staticmethod(lambda *args: None)
        )
        setup = staticmethod(lambda *args: {})
        build_prompt = staticmethod(lambda *args: "public question")
        verify = staticmethod(lambda *args, **kw: {"reward": 1, "method": "llm_judge"})

    monkeypatch.setattr(
        domain_worker,
        "run_runtime",
        lambda *args: {
            "result": "Exact Answer: answer",
            "tracelog": {"status": "succeeded"},
        },
    )
    result = domain_worker.trial(
        Adapter(),
        {"name": "1"},
        {
            "settings": {"domain": "information_retrieval", "runner": "runtime"},
            "job_tag": "unique",
        },
    )
    assert result["tracelog"]["verifier"]["score"] == 1


def test_judge_parse_error_does_not_become_task_zero(tmp_path: Path) -> None:
    path = tmp_path / "verifier"
    path.mkdir()
    (path / "details.json").write_text(
        '{"judge_result":{"parse_error":true,"error":"transport failure"},'
        '"reward":0,"method":"exact_match"}'
    )
    with pytest.raises(RuntimeError, match="judge"):
        domain_worker.check_retrieval_judge(tmp_path)
