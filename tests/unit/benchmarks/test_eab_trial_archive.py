"""本地中断恢复与安全会话索引的协议测试。"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from benchmarks.evoagentbench.common.process import invoke
from benchmarks.evoagentbench.common.trace_store import TrialStore
from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.domains import domain_adapter
from benchmarks.evoagentbench.domains.domain_settings import DomainSettings
from evolution.models import CaseAttempt, DatasetRole, ExecutionMetadata


def _attempt(status: str = "succeeded") -> CaseAttempt:
    return CaseAttempt(
        result="output",
        metadata=ExecutionMetadata(total_tokens=2, duration_seconds=1),
        tracelog={
            "status": status,
            "protocol": "p",
            "target_sha256": "t",
            "messages": {"session_id": "session"},
        },
    )


def _store(root: Path, trial: int = 0, protocol: str = "p") -> TrialStore:
    return TrialStore(
        root,
        protocol=protocol,
        target_sha256="t",
        case_id="source-uuid",
        trial_index=trial,
        candidate="candidate",
    )


def test_latest_failure_blocks_older_success_and_trials_stay_distinct(
    tmp_path: Path,
) -> None:
    _store(tmp_path).save("succeeded", execution=_attempt())
    assert _store(tmp_path).resume() == _attempt()
    _store(tmp_path).save("failed", diagnostic={"exception_type": "TimeoutError"})
    assert _store(tmp_path).resume() is None
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda n: _store(tmp_path, n).save("succeeded", execution=_attempt()),
                range(1, 5),
            )
        )
    assert len(list(tmp_path.rglob("*.json"))) == 6
    assert _store(tmp_path, 5).resume() is None
    assert _store(tmp_path, 1, "other").resume() is None


def test_tampered_identity_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("succeeded", execution=_attempt())
    record = json.loads(store.path.read_text())
    record["identity"]["task_id"] = "other"
    store.path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="identity mismatch"):
        _store(tmp_path).resume()


def test_executor_recovers_completed_samples_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = DomainSettings(
        domain="information_retrieval",
        upstream_repo=tmp_path,
        upstream_python=Path(sys.executable),
        domain_config=tmp_path / "domain.yaml",
        runtime_config=tmp_path / "runtime.yaml",
        artifact_dir=tmp_path / "archive",
        test_trials=3,
    )
    manifest = tmp_path / "dataset.json"
    manifest.write_text(
        Dataset(
            role="test",
            upstream_revision=settings.upstream_revision,
            source_sha256="source",
            cases=(Case(id="uuid", prompt="public"),),
        ).model_dump_json()
    )
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(domain_adapter, "experiment_identity", lambda *_: "p")
    calls = 0

    def worker(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("private exception must not be persisted")
        return _attempt().model_dump(mode="json")

    monkeypatch.setattr(domain_adapter, "invoke", worker)
    executor = domain_adapter.DomainExecutor(settings)
    with pytest.raises(TimeoutError):
        executor.execute(target, manifest, DatasetRole.TEST, max_retries=0)
    result = executor.execute(target, manifest, DatasetRole.TEST, max_retries=0)
    assert calls == 4
    assert len(result.cases[0].trials) == 3
    assert settings.artifact_dir is not None
    records = [json.loads(p.read_text()) for p in settings.artifact_dir.rglob("*.json")]
    assert sorted(r["status"] for r in records) == [
        "failed",
        "succeeded",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    # 恢复缓存采样时，也应记录恢复执行的回执。
    assert sorted(r["identity"]["trial_index"] for r in records) == [0, 0, 1, 1, 2]
    assert any(
        r["diagnostic"]
        == {
            "exception_type": "TimeoutError",
            "process": None,
        }
        for r in records
    )
    assert "private exception" not in json.dumps(records)


def test_timeout_has_durable_safe_diagnostic() -> None:
    with pytest.raises(TimeoutError) as caught:
        invoke([sys.executable, "-c", "import time; time.sleep(10)"], {}, timeout=0.05)
    path = Path(vars(caught.value)["diagnostic_path"])
    try:
        assert json.loads(path.read_text())["exception_type"] == "TimeoutError"
    finally:
        path.unlink()


def test_worker_failure_diagnostic_retains_frames_but_not_exception_text() -> None:
    with pytest.raises(RuntimeError) as caught:
        invoke(
            [sys.executable, "-c", "raise ValueError('do-not-store-hidden-data')"],
            {},
            timeout=5,
        )
    path = Path(vars(caught.value)["diagnostic_path"])
    try:
        text = path.read_text()
        record = json.loads(text)
        assert record["worker_exception_type"] == "ValueError"
        assert record["frames"]
        assert "do-not-store-hidden-data" not in text
    finally:
        path.unlink()
