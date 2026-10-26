"""DB feedback 保留官方 hash 时刻的字段差异。"""

import json
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from evolution_tasks.tau3_banking._internal.runner import capture_db_checks
from pydantic import JsonValue


class FakeEnvironment:
    """模拟 Tau2 的数据库快照和 hash 接口。"""

    def __init__(self, state: dict[str, JsonValue], database: str = "agent") -> None:
        self.state = state
        toolkit = SimpleNamespace(db=SimpleNamespace(model_dump=lambda: self.state))
        self.tools = toolkit if database == "agent" else None
        self.user_tools = toolkit if database == "user" else None

    def get_db_hash(self) -> str | None:
        """使用与官方相同的 JSON 比较语义。"""
        return (
            json.dumps(self.state, sort_keys=True, default=str) if self.tools else None
        )

    def get_user_db_hash(self) -> str | None:
        """未提供 user DB 时返回 None。"""
        return json.dumps(self.state, sort_keys=True) if self.user_tools else None


@pytest.mark.parametrize("matches", [True, False])
@pytest.mark.parametrize("database", ["agent", "user"])
def test_capture_differences_at_hash_time_and_preserve_reward(
    matches: bool, database: Literal["agent", "user"]
) -> None:
    """新增/缺失、null、数字类型和列表顺序差异都能定位。"""
    expected: dict[str, JsonValue] = {
        "amount": 5,
        "calls": ["a", "b"],
        "missing": None,
        "a/b~c": True,
    }
    actual: dict[str, JsonValue] = (
        expected.copy()
        if matches
        else {"amount": 5.0, "calls": ["b", "a", "c"], "extra": None, "a/b~c": 1}
    )
    environments = [
        FakeEnvironment(actual, database),
        FakeEnvironment(expected, database),
    ]
    constructed = []

    def constructor(**kwargs: object) -> FakeEnvironment:
        constructed.append(kwargs)
        return environments[len(constructed) - 1]

    reward = SimpleNamespace(
        db_check=SimpleNamespace(db_match=matches, db_reward=float(matches))
    )

    class Evaluator:
        @classmethod
        def calculate_reward(
            cls, *, environment_constructor: Any, **kwargs: object
        ) -> object:
            assert kwargs == {"task": "original"}
            predicted = environment_constructor(solo_mode=False)
            gold = environment_constructor()
            gold.get_db_hash()
            gold.get_user_db_hash()
            predicted.get_db_hash()
            predicted.get_user_db_hash()
            # 环境断言发生在 hash 比较之后，不能污染诊断快照。
            actual["after_hash"] = "ignore"
            return reward

    original = vars(Evaluator)["calculate_reward"]
    with capture_db_checks(Evaluator) as checks:
        result = Evaluator.calculate_reward(
            environment_constructor=constructor, task="original"
        )
    assert result is reward
    assert vars(Evaluator)["calculate_reward"] is original
    assert constructed == [{"solo_mode": False}, {}]
    assert len(checks) == 1
    check = checks[0]
    assert check.db_match is matches
    assert check.db_reward == float(matches)
    if matches:
        assert check.differences == []
        assert check.diagnosis_status == "matched"
    else:
        differences = {item.path: item for item in check.differences}
        assert set(differences) == {
            "/amount",
            "/calls/0",
            "/calls/1",
            "/calls/2",
            "/missing",
            "/extra",
            "/a~1b~0c",
        }
        assert differences["/amount"].expected == 5
        assert type(differences["/amount"].actual) is float
        assert differences["/extra"].expected_present is False
        assert differences["/extra"].actual_present is True
        assert differences["/missing"].expected_present is True
        assert differences["/missing"].actual_present is False
        assert differences["/calls/2"].expected_present is False
        assert all(item.database == database for item in check.differences)
        assert check.diagnosis_status == "differences_found"


def test_capture_restores_evaluator_when_official_evaluation_raises() -> None:
    """官方重放失败仍然传播，且不会留下 hook。"""

    class Evaluator:
        @classmethod
        def calculate_reward(cls, **kwargs: object) -> object:
            raise ValueError("replay failed")

    original = vars(Evaluator)["calculate_reward"]
    with pytest.raises(ValueError, match="replay failed"), capture_db_checks(Evaluator):
        Evaluator.calculate_reward(environment_constructor=lambda: None)
    assert vars(Evaluator)["calculate_reward"] is original


@pytest.mark.parametrize("db_match", [None, True, False])
def test_no_environment_snapshots_are_not_invented(db_match: bool | None) -> None:
    """无 DB 评分不伪造诊断，缺快照不能被报告为已定位。"""

    class Evaluator:
        @classmethod
        def calculate_reward(cls, **kwargs: object) -> object:
            return SimpleNamespace(
                db_check=None
                if db_match is None
                else SimpleNamespace(db_match=db_match, db_reward=float(db_match))
            )

    with capture_db_checks(Evaluator) as checks:
        Evaluator.calculate_reward(environment_constructor=lambda: None)
    if db_match is None:
        assert checks == []
    else:
        assert checks[0].diagnosis_status == ("matched" if db_match else "unavailable")
        assert checks[0].differences == []
