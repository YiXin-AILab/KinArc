"""办公反馈只报告证据，不编造失败原因或分数。"""

import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.domains import domain_worker
from benchmarks.evoagentbench.domains.domain_adapter import DomainEvaluator
from benchmarks.evoagentbench.domains.domain_settings import (
    DomainSettings,
    experiment_identity,
)
from evolution.diagnosis import build_diagnosis_brief
from evolution.models import (
    CaseExecution,
    ExecutionMetadata,
    ExecutionResult,
)


@pytest.mark.parametrize(
    "answer,extra,verdict,kind,observation",
    [
        ("", {"artifacts": []}, {}, "office.empty_answer_no_archived_files", "empty"),
        ("", {}, {}, "unclassified_failure", "unavailable"),
        (
            "done",
            {"artifacts": []},
            {"error": "no_artifacts_found"},
            "office.no_artifacts",
            "no_artifacts_found",
        ),
        (
            "done",
            {"artifacts": [{"name": "build.py", "bytes": 4}]},
            {},
            "unclassified_failure",
            "not proof",
        ),
        ("done", {}, {}, "unclassified_failure", "unavailable"),
    ],
)
def test_failure_evidence_reaches_diagnosis_without_changing_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    extra: dict[str, Any],
    verdict: dict[str, Any],
    kind: str,
    observation: str,
) -> None:
    monkeypatch.setattr(
        "benchmarks.evoagentbench.domains.domain_settings.dependency_identity",
        lambda _: "fixed",
    )
    config = tmp_path / "config"
    config.write_text("fixed")
    settings = DomainSettings(
        domain="knowledge_work",
        upstream_repo=tmp_path,
        upstream_python=tmp_path / "python",
        domain_config=config,
        runtime_config=config,
        train_trials=1,
    )
    path = tmp_path / "train.json"
    path.write_text(
        Dataset(
            role="train",
            upstream_revision=settings.upstream_revision,
            source_sha256="fixed",
            cases=(Case(id="a", prompt="public"),),
        ).model_dump_json()
    )
    trace = {
        "status": "succeeded",
        "protocol": experiment_identity(settings, path),
        "verifier": {"score": 0.5, "passed": False, "pass_threshold": 0.6, **verdict},
        **extra,
    }
    case = CaseExecution(
        id="a",
        result=answer,
        tracelog=trace,
        metadata=ExecutionMetadata(total_tokens=1, duration_seconds=1),
    )
    result = DomainEvaluator(settings).evaluate(ExecutionResult(cases=(case,)), path)
    row = result.cases[0]
    assert result.total_score == row.score == 0
    assert row.failure_kind == kind
    assert observation in row.feedback
    assert build_diagnosis_brief("candidate", result).clusters[0].signature == kind
    trace["verifier"] = {"score": 0.7, "passed": True, "pass_threshold": 0.6}
    passed = DomainEvaluator(settings).evaluate(
        ExecutionResult(cases=(case.model_copy(update={"tracelog": trace}),)),
        path,
    )
    assert passed.total_score == 1
    assert passed.cases[0].failure_kind is None


def test_normalization_keeps_only_allowlisted_error() -> None:
    verdict = domain_worker.normalized_verdict(
        {"reward": 0, "error": "no_artifacts_found", "rubric": "PRIVATE"}, 0.6
    )
    assert verdict["error"] == "no_artifacts_found"
    assert "PRIVATE" not in json.dumps(verdict)


def test_archive_checks_files_without_treating_scripts_as_deliverables(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "broken.xlsx").write_text("not a zip")
    (workspace / "empty.docx").touch()
    (workspace / "build.py").write_text("raise Exception('must not execute')")
    with ZipFile(workspace / "missing.pptx", "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
    with ZipFile(workspace / "ok.xlsx", "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("xl/workbook.xml", "<workbook/>")
    result: dict[str, Any] = {"result": "done", "tracelog": {"status": "succeeded"}}
    domain_worker.archive_outputs(
        {"settings": {"artifact_dir": str(tmp_path / "archive")}, "job_tag": "job"},
        {"workspace_dir": str(workspace)},
        result,
    )
    checks = {
        entry["name"]: entry["check"] for entry in result["tracelog"]["artifacts"]
    }
    assert checks["broken.xlsx"] == "invalid_office_zip"
    assert checks["empty.docx"] == "empty_file"
    assert checks["missing.pptx"] == "missing_office_part"
    assert checks["ok.xlsx"] == "office_main_parts_readable"
    assert checks["build.py"] == "not_checked"


def test_package_checks_bound_decompression(tmp_path: Path) -> None:
    from benchmarks.evoagentbench.domains.office_feedback import check_archived_file

    path = tmp_path / "large.xlsx"
    with ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("xl/workbook.xml", "x" * (2 * 1024 * 1024 + 1))
    assert check_archived_file(path) == "office_check_limit_exceeded"


@pytest.mark.parametrize("error", [None, "judge failed"])
def test_worker_preserves_private_verdict_and_normalized_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: str | None,
) -> None:
    raw = {"reward": 0.5, "rubric": "PRIVATE RUBRIC", "error": error}

    class Adapter:
        pass_threshold = staticmethod(lambda: 0.6)
        initialize = staticmethod(lambda *args: None)
        pre_task_trials = staticmethod(lambda *args: None)
        setup = staticmethod(lambda *args: {})
        build_prompt = staticmethod(lambda *args: "public task")
        verify = staticmethod(lambda *args, **kwargs: raw)
        cleanup = staticmethod(lambda *args: None)
        post_task_trials = staticmethod(lambda *args: None)
        finalize = staticmethod(lambda *args: None)

    monkeypatch.setattr(
        domain_worker,
        "run_runtime",
        lambda *args: {"result": "public answer", "tracelog": {"status": "succeeded"}},
    )
    request: dict[str, Any] = {
        "settings": {
            "domain": "information_retrieval",
            "runner": "runtime",
            "artifact_dir": str(tmp_path),
        },
        "job_tag": "job",
    }
    if error:
        with pytest.raises(RuntimeError, match="Official evaluator failed"):
            domain_worker.trial(Adapter(), {"name": "case"}, request)
    else:
        result = domain_worker.trial(Adapter(), {"name": "case"}, request)
        assert result["tracelog"]["verifier"]["score"] == 0.5
    private = tmp_path / "outputs/job/official-verdict.private.json"
    assert json.loads(private.read_text()) == raw
    assert private.stat().st_mode & 0o777 == 0o600
    archived = (tmp_path / "outputs/job/execution.json").read_text()
    assert "PRIVATE RUBRIC" not in archived
    if not error:
        assert json.loads(archived)["tracelog"]["verifier"]["score"] == 0.5


@pytest.mark.parametrize(
    "xml", ["<broken", '<!DOCTYPE x [<!ENTITY y "text">]><x>&y;</x>']
)
def test_invalid_xml_and_entities_are_not_readable(tmp_path: Path, xml: str) -> None:
    from benchmarks.evoagentbench.domains.office_feedback import check_archived_file

    path = tmp_path / "document.docx"
    with ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", xml)
    assert check_archived_file(path) == "invalid_office_xml"


def test_mixed_failure_symptoms_stay_unknown_and_passes_do_not_add_failures() -> None:
    from benchmarks.evoagentbench.domains.office_feedback import summarize_office_trials

    verdict = {"score": 0, "passed": False, "pass_threshold": 0.6}
    missing = ("done", {"verifier": {**verdict, "error": "no_artifacts_found"}})
    unknown = ("done", {"verifier": verdict})
    empty = ("", {"verifier": verdict, "artifacts": []})
    passed = ("", {"verifier": {**verdict, "score": 0.9, "passed": True}})
    assert summarize_office_trials([missing, unknown])[1] == "unclassified_failure"
    assert summarize_office_trials([missing, empty])[1] == "unclassified_failure"
    assert summarize_office_trials([missing, passed])[1] == "office.no_artifacts"
    assert summarize_office_trials([passed])[1] is None
