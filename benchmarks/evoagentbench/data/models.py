"""经过校验的进程通信与数据集协议。"""

from typing import Literal

from agentsystem.common.models import StrictModel
from pydantic import Field

UPSTREAM_REVISION = "948a17288782d5120778da16b4cf1cad9305d8b4"


class Case(StrictModel):
    """仅允许公开任务资料进入候选执行进程。"""

    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


class Dataset(StrictModel):
    """绑定官方来源的不可变派生数据集划分。"""

    role: Literal["train", "validation", "test"]
    upstream_revision: str
    source_sha256: str
    cases: tuple[Case, ...]
