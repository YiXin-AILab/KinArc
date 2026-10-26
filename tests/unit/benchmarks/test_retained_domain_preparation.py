"""准备入口仅导出所选办公或检索数据划分。"""

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks.evoagentbench.data import prepare_dataset
from benchmarks.evoagentbench.data.models import Dataset
from benchmarks.evoagentbench.domains import domain_adapter
from benchmarks.evoagentbench.domains.domain_settings import DomainSettings
from benchmarks.evoagentbench.experiments import vanilla


@pytest.mark.parametrize("domain", ["knowledge_work", "information_retrieval"])
@pytest.mark.parametrize("wrong_membership", [False, True])
def test_prepare_selected_official_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    wrong_membership: bool,
) -> None:
    settings = DomainSettings.model_validate(
        {
            "domain": domain,
            "upstream_repo": tmp_path,
            "upstream_python": tmp_path / "python",
            "domain_config": tmp_path / "domain.yaml",
            "runtime_config": tmp_path / "runtime.yaml",
        }
    )
    split = tmp_path / "benchmark/data/splits" / f"{domain}.json"
    split.parent.mkdir(parents=True)
    original = {"train": ["a", "b", "c"], "test": ["held-out"]}
    split.write_text(json.dumps(original))
    (tmp_path / "config.yaml").write_text(
        "objective: fixture\ninitial_target: agent\nworkspace: {}\nsettings: {}\n"
    )
    monkeypatch.setattr(prepare_dataset, "settings_for", lambda *_: settings)
    calls = []

    def export(
        actual: DomainSettings, operation: str, ids: list[str]
    ) -> dict[str, Any]:
        assert actual.domain == domain and operation == "prepare"
        calls.append(list(ids))
        return {
            "cases": [
                {"id": "unexpected" if wrong_membership else key, "prompt": key}
                for key in ids
            ]
        }

    monkeypatch.setattr(prepare_dataset, "upstream", export)
    if wrong_membership:
        with pytest.raises(ValueError, match="membership"):
            prepare_dataset.prepare(tmp_path, 1, 42, "formal")
        assert not (tmp_path / "datasets").exists()
        return
    prepare_dataset.prepare(tmp_path, 1, 42, "formal")
    roles = prepare_dataset.split_ids(original, 1, 42)
    assert calls == list(roles.values())
    for role, ids in roles.items():
        dataset = Dataset.model_validate_json(
            (tmp_path / "datasets" / f"{role}.json").read_text()
        )
        assert [case.id for case in dataset.cases] == ids
    with pytest.raises(FileExistsError):
        prepare_dataset.prepare(tmp_path, 1, 42, "formal")
    assert len(calls) == 3
    monkeypatch.setattr(domain_adapter, "upstream", export)
    assert [case.id for case in vanilla.official_train(settings).cases] == original[
        "train"
    ]
