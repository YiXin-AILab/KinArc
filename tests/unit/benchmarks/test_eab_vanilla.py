"""原始模型对比不得丢弃错误，也不得静默使用其他模型密钥。"""

import json
from pathlib import Path
from threading import Barrier, Lock

import pytest
from benchmarks.evoagentbench.common.identity import file_digest
from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.experiments import vanilla
from benchmarks.evoagentbench.experiments.experiment_state import write_json
from evolution.models import CaseEvaluation, EvaluationResult, ExecutionMetadata


def test_profile_requires_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAB_API_KEY", "other-model-key")
    profile = tmp_path / "model.env"
    profile.write_text(
        "EAB_MODEL=openai/example\nEAB_BASE_MODEL=example\nEAB_API_KEY=\nEAB_BASE_URL=https://example.invalid\n"
    )
    with pytest.raises(ValueError, match="EAB_API_KEY"):
        vanilla.load_profile(profile)


def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dataset = Dataset(
        role="train",
        upstream_revision="fixture",
        source_sha256="fixture",
        cases=(Case(id="a", prompt="a"), Case(id="b", prompt="b")),
    )
    write_json(tmp_path / "train.json", dataset.model_dump(mode="json"))
    write_json(tmp_path / "vanilla.json", {"fixture": True})
    monkeypatch.setattr(vanilla, "conditions", lambda *_: {"fixture": True})
    return tmp_path


def complete(output: Path, case_id: str, score: float) -> None:
    case = CaseEvaluation(
        id=case_id,
        score=score,
        result="fixture",
        expected=None,
        feedback="fixture",
        tracelog={},
        metadata=ExecutionMetadata(total_tokens=1, duration_seconds=1),
    )
    write_json(output / "execution.json", {})
    write_json(
        output / "evaluation.json",
        EvaluationResult(total_score=score, cases=(case,)).model_dump(mode="json"),
    )
    write_json(
        output / "receipt.json",
        {
            "execution_sha256": file_digest(output / "execution.json"),
            "evaluation_sha256": file_digest(output / "evaluation.json"),
        },
    )


def test_errors_do_not_stop_other_cases_and_resume_skips_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared(tmp_path, monkeypatch)
    called = []

    def execute(root: Path, path: Path, arm: str, output: Path) -> None:
        case = Dataset.model_validate_json(path.read_text()).cases[0]
        called.append((arm, case.id))
        if arm == "base" and case.id == "a":
            raise TimeoutError("fixture")
        complete(output, case.id, 0 if arm == "yx" else 1)

    monkeypatch.setattr(vanilla, "run_case", execute)
    vanilla.run(root, ["base", "yx"])
    assert len(called) == 4
    result = vanilla.report(root)
    assert result["arms"]["base"]["pass_at_1"] is None
    assert result["arms"]["base"]["errors"] == 1
    assert result["arms"]["yx"]["pass_at_1"] == 0
    called.clear()
    vanilla.run(root, ["base", "yx"])
    assert called == [("base", "a")]


def test_resume_rejects_changed_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(vanilla, "conditions", lambda *_: {"fixture": False})
    with pytest.raises(ValueError, match="conditions changed"):
        vanilla.run(root, ["yx"])


def test_receipt_rejects_changed_scores(tmp_path: Path) -> None:
    complete(tmp_path, "a", 1)
    value = json.loads((tmp_path / "evaluation.json").read_text())
    value["total_score"] = 0
    write_json(tmp_path / "evaluation.json", value)
    with pytest.raises(ValueError, match="evidence changed"):
        vanilla.cached_result(tmp_path, "a")


def test_concurrency_is_shared_by_arms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared(tmp_path, monkeypatch)
    barrier = Barrier(2)
    lock = Lock()
    active = 0
    peak = 0
    seen = []

    def execute(root: Path, path: Path, arm: str, output: Path) -> None:
        nonlocal active, peak
        case = Dataset.model_validate_json(path.read_text()).cases[0]
        with lock:
            active += 1
            peak = max(peak, active)
            seen.append((case.id, arm))
        barrier.wait(timeout=5)
        complete(output, case.id, 1)
        with lock:
            active -= 1

    monkeypatch.setattr(vanilla, "run_case", execute)
    vanilla.run(root, ["base", "yx"], concurrency=2)
    assert peak == 2
    assert set(seen) == {(case, arm) for case in ("a", "b") for arm in ("base", "yx")}
    assert vanilla.report(root)["arms"]["base"]["pass_at_1"] == 1


def test_invalid_concurrency_has_no_side_effects(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        vanilla.run(tmp_path, ["base", "yx"], concurrency=0)
    assert not list(tmp_path.iterdir())
