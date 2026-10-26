"""原子保存单次试验证据，供中断恢复时校验身份。"""

import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from evolution.models import CaseAttempt


def _atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class TrialStore:
    """分开保存执行尝试，仅复用最新且身份匹配的成功尝试。"""

    def __init__(
        self,
        root: Path,
        *,
        protocol: str,
        target_sha256: str,
        case_id: str,
        trial_index: int,
        candidate: str,
    ) -> None:
        """绑定一次试验的身份，并为新执行尝试预留归档文件名。

        Args:
            root: 保存试次归档的私有产物目录。
            protocol: 冻结执行及评分条件的摘要。
            target_sha256: 标识候选或基线配置的摘要。
            case_id: 数据集任务标识。
            trial_index: 案例内从零开始的独立采样序号。
            candidate: 恢复身份中包含的候选目录名称。
        """
        self.identity = {
            "protocol": protocol,
            "target_sha256": target_sha256,
            "task_id": case_id,
            "trial_index": trial_index,
            "candidate": candidate,
        }
        key = hashlib.sha256(
            json.dumps(self.identity, sort_keys=True).encode()
        ).hexdigest()
        self.directory = root / "trials" / key
        self.path = self.directory / f"{time.time_ns():020d}-{uuid4().hex}.json"

    def resume(self) -> CaseAttempt | None:
        """拒绝损坏或不匹配的归档，不静默信任无效证据。

        Returns:
            返回最新且身份匹配的成功执行；没有归档，
            或最新尝试未成功时返回 None。

        Raises:
            ValueError: 归档结构或执行身份不一致。
        """
        paths = sorted(self.directory.glob("*.json"))
        if not paths:
            return None
        record = json.loads(paths[-1].read_text(encoding="utf-8"))
        if record.get("schema_version") != 1 or record.get("identity") != self.identity:
            raise ValueError("Trial archive identity mismatch")
        if record["status"] != "succeeded":
            return None
        trial = CaseAttempt.model_validate(record["execution"])
        if (
            not isinstance(trial.tracelog, dict)
            or any(
                trial.tracelog.get(key) != self.identity[key]
                for key in ("protocol", "target_sha256")
            )
            or trial.tracelog.get("status") != "succeeded"
        ):
            raise ValueError("Trial execution identity mismatch")
        return trial

    def save(
        self,
        status: str,
        *,
        execution: CaseAttempt | None = None,
        diagnostic: dict[str, object] | None = None,
    ) -> None:
        """原子更新一次执行尝试，保留更早的尝试和采样记录。

        Args:
            status: 执行尝试状态，例如 running、failed 或 succeeded。
            execution: 包含公开执行轨迹的可选已完成执行记录。
            diagnostic: 可选的安全失败信息，不包含原始 worker 输出。
        """
        session_id = None
        if execution is not None and isinstance(execution.tracelog, dict):
            messages = execution.tracelog.get("messages")
            if isinstance(messages, dict):
                identifier = messages.get("session_id")
                if isinstance(identifier, str):
                    session_id = identifier
        record: dict[str, object] = {
            "schema_version": 1,
            "identity": self.identity,
            "status": status,
            "session_id": session_id,
            "diagnostic": diagnostic,
            "execution": execution.model_dump(mode="json") if execution else None,
        }
        _atomic(self.path, record)
