"""SQL ScheduleManager 的固定长度摘要键。"""

import hashlib


def occurrence_key(task_id: str, occurrence: str) -> bytes:
    """无歧义计算 task occurrence 唯一键。

    Args:
        task_id: 目标定时任务 ID。
        occurrence: 任务内唯一的触发标识。

    Returns:
        SHA-256 摘要的 32 字节二进制值。
    """
    digest = hashlib.sha256()
    for value in (task_id, occurrence):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


__all__ = ["occurrence_key"]
