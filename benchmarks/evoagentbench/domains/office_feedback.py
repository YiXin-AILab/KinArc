"""提供有界且不参与评分的办公观察，不推测评分细则或失败原因。"""

from collections import Counter
from pathlib import Path
from typing import Any
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

_MAIN_PARTS = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_PART_BYTES = 2 * 1024 * 1024
_MAX_ENTRIES = 10_000
_UNKNOWN = "unclassified_failure"


def _reject_doctype(*args: object) -> None:
    raise ValueError("DTD is outside the office diagnostic contract")


def _check_parts(package: ZipFile, main: str) -> str:
    if len(package.infolist()) > _MAX_ENTRIES:
        return "office_check_limit_exceeded"
    for name in ("[Content_Types].xml", main):
        try:
            part = package.getinfo(name)
        except KeyError:
            return "missing_office_part"
        if part.file_size > _MAX_PART_BYTES:
            return "office_check_limit_exceeded"
        with package.open(part) as stream:
            content = stream.read(_MAX_PART_BYTES + 1)
        if len(content) > _MAX_PART_BYTES:
            return "office_check_limit_exceeded"
        parser = expat.ParserCreate()
        parser.StartDoctypeDeclHandler = _reject_doctype
        parser.Parse(content, True)
    return "office_main_parts_readable"


def check_archived_file(path: Path) -> str:
    """检查 Office 文件主要组件，不解压文件，也不执行文件内容。

    主要组件可读取并不能证明文档包完整、公式正确、
    渲染质量合格、任务已完成，或文件属于最终交付物。
    检查错误不得覆盖官方分数或使其失效。
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return "empty_file"
        main = _MAIN_PARTS.get(path.suffix.lower())
        if main is None:
            return "not_checked"
        if size > _MAX_FILE_BYTES:
            return "office_check_limit_exceeded"
        with ZipFile(path) as package:
            return _check_parts(package, main)
    except BadZipFile:
        return "invalid_office_zip"
    except (expat.ExpatError, ValueError):
        return "invalid_office_xml"
    except (OSError, RuntimeError, NotImplementedError):
        return "office_check_unavailable"


def summarize_office_trials(
    trials: list[tuple[object, dict[str, Any]]],
) -> tuple[str, str | None]:
    """描述已校验的官方试次，并保守分类已观测现象。

    仅在协议、执行状态和官方评分证据校验通过后调用。
    旧回执缺少归档索引时，产物是否缺失仍未知；
    归档也可能包含中间文件，因此不能据此判定任务交付情况。
    """
    observations = []
    kinds = set()
    for number, (answer, trace) in enumerate(trials, start=1):
        verdict = trace["verifier"]
        empty = isinstance(answer, str) and not answer.strip()
        answer_state = (
            "empty"
            if empty
            else "present"
            if isinstance(answer, str)
            else "unavailable"
        )
        artifacts = trace.get("artifacts")
        count = len(artifacts) if isinstance(artifacts, list) else None
        observation = (
            f"Trial {number}: official reward={verdict['score']}, "
            f"passed={verdict['passed']}, threshold={verdict['pass_threshold']}; "
            f"final answer={answer_state}. "
        )
        if isinstance(artifacts, list):
            checks = Counter(
                entry.get("check", "not_recorded")
                for entry in artifacts
                if isinstance(entry, dict)
                and isinstance(entry.get("check", "not_recorded"), str)
            )
            observation += (
                f"Archived files={count}, checks={dict(checks)}; "
                "not proof of delivery completeness or correctness. "
            )
        else:
            observation += (
                "Archive evidence unavailable; do not infer missing deliverables. "
            )
        kind = _UNKNOWN
        if verdict.get("error") == "no_artifacts_found":
            observation += "Official verifier reported no_artifacts_found. "
            kind = "office.no_artifacts"
        elif empty and count == 0:
            kind = "office.empty_answer_no_archived_files"
        if not verdict["passed"]:
            kinds.add(kind)
            observation += "Failure cause is not established by these observations."
        observations.append(observation.strip())
    # 不同观测现象或包含未知试次时，不能归为同一因果簇。
    failure_kind = next(iter(kinds)) if len(kinds) == 1 else _UNKNOWN if kinds else None
    return "\n".join(observations), failure_kind
