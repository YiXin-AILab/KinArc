"""恢复来源模型的边界校验。"""

import pytest
from evolution.models import RollbackRecord
from pydantic import ValidationError


def _record() -> dict[str, object]:
    return {
        "source_candidate_id": "candidate-20260917T120000",
        "changelog_source": "best",
        "changelog_source_candidate_id": "candidate-20260917T120000",
        "reason": "exploration_patience_exhausted",
        "exploration_patience": 3,
        "exploration_depth": 3,
        "source_target_sha256": "a" * 64,
        "reused_artifacts": {
            role: {
                "execution_sha256": "b" * 64,
                "evaluation_sha256": "c" * 64,
            }
            for role in ("train", "validation")
        },
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"reason": "unknown"},
        {"exploration_depth": 2},
        {"exploration_depth": None},
        {"exploration_depth": True},
        {"exploration_patience": "3"},
        {"source_target_sha256": "invalid"},
        {"unknown": 1},
        {"reused_artifacts": {}},
        {"source_candidate_id": "../other"},
        {"reason": "anchor_not_in_lineage", "exploration_depth": 0},
    ],
)
def test_rollback_rejects_invalid_record(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RollbackRecord.model_validate(_record() | updates)


@pytest.mark.parametrize("roles", [("train",), ("train", "validation", "test")])
def test_rollback_requires_exact_roles(roles: tuple[str, ...]) -> None:
    record = _record()
    record["reused_artifacts"] = {
        role: {
            "execution_sha256": "b" * 64,
            "evaluation_sha256": "c" * 64,
        }
        for role in roles
    }
    with pytest.raises(ValidationError):
        RollbackRecord.model_validate(record)


@pytest.mark.parametrize("missing_anchor", [False, True])
def test_rollback_round_trip(missing_anchor: bool) -> None:
    data = _record()
    if missing_anchor:
        data.update(reason="anchor_not_in_lineage", exploration_depth=None)
    record = RollbackRecord.model_validate(data)
    assert RollbackRecord.model_validate_json(record.model_dump_json()) == record
