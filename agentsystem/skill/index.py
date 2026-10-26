"""按应用优先级构建的启动期 SkillIndex。"""

import asyncio
import codecs
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Event, Lock
from types import MappingProxyType
from typing import cast

# cachetools does not ship typing metadata in the pinned release.
from cachetools import TTLCache  # type: ignore[import-untyped]

from agentsystem.skill.models import (
    SkillCatalogEntry,
    SkillSource,
    SkillValidationResult,
)
from agentsystem.skill.validator import NAME_RE, SkillValidator

_CACHE: TTLCache[tuple[Path, int, int, int], tuple[str, int]] = TTLCache(
    maxsize=1_024,
    ttl=60,
)
_CACHE_LOCK = Lock()


class SkillIndex:
    """保存启动阶段已启用 Skill 的元数据与资源发现清单。

    索引复制输入映射后只读使用；不自动刷新磁盘内容，也不约束运行期读取路径。
    """

    def __init__(self, records: Mapping[str, SkillValidationResult]) -> None:
        """从已校验记录创建只读索引。

        Args:
            records: 按启用顺序排列、以 Skill 名称为键的校验结果。
        """
        self._records = MappingProxyType(dict(records))

    @classmethod
    def load(
        cls,
        *,
        roots: Sequence[Path],
        enabled_skills: Sequence[str],
        validator: SkillValidator | None = None,
    ) -> "SkillIndex":
        """扫描并校验启用的 Skill，首个同名目录获胜。

        Args:
            roots: 按应用到全局优先级排列的根目录。
            enabled_skills: 要加载的唯一 Skill 名称及展示顺序。
            validator: 可替换的启动期校验器，缺省时使用 SkillValidator。

        Returns:
            完成全部磁盘校验后的只读索引。

        Raises:
            ValueError: 名称重复、格式无效或 Skill 内容校验失败。
            KeyError: 启用的 Skill 在全部根目录中均不存在。
        """
        if len(set(enabled_skills)) != len(enabled_skills):
            raise ValueError("enabled_skills 包含重复名称")
        checker = validator or SkillValidator()
        records: dict[str, SkillValidationResult] = {}
        for name in enabled_skills:
            if NAME_RE.fullmatch(name) is None:
                raise ValueError(f"enabled skill 名称无效：{name!r}")
            directory, source = _resolve_directory(name, roots)
            result = checker.validate(directory, source)
            records[name] = result
        return cls(records)

    def names(self) -> tuple[str, ...]:
        """按 enabled_skills 配置顺序返回已激活名称。"""
        return tuple(self._records)

    def catalog(self) -> tuple[SkillCatalogEntry, ...]:
        """返回不包含 Skill 正文的紧凑目录。"""
        return tuple(
            SkillCatalogEntry(
                name=record.name,
                description=record.description,
                linked_files=tuple(
                    path for path in record.resources if path != "SKILL.md"
                ),
                metadata=record.metadata,
            )
            for record in self._records.values()
        )

    def get(self, name: str) -> SkillValidationResult | None:
        """按名称返回已校验快照。

        Args:
            name: 已启用的 Skill 名称。

        Returns:
            启动时的校验记录；未启用时返回 None。
        """
        return self._records.get(name)


def _resolve_directory(
    name: str,
    roots: Sequence[Path],
) -> tuple[Path, SkillSource]:
    for index, root in enumerate(roots):
        candidate = Path(root) / name
        try:
            candidate.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise ValueError(f"Skill {name!r} 路径无法读取") from error
        source = SkillSource.APPLICATION if index == 0 else SkillSource.GLOBAL
        return candidate, source
    raise KeyError(name)


def _read_text(
    path: Path,
    offset: int,
    limit: int,
    cancelled: Event,
) -> tuple[str, int]:
    with path.open("rb") as stream:
        key = (path.absolute(), os.fstat(stream.fileno()).st_mtime_ns, offset, limit)
        with _CACHE_LOCK:
            cached = cast(tuple[str, int] | None, _CACHE.get(key))
        if cached is not None:
            return cached
        page = _scan_text(stream.fileno(), offset, limit, cancelled)
        if cancelled.is_set():
            raise asyncio.CancelledError
        with _CACHE_LOCK:
            _CACHE[key] = page
        return page


def _scan_text(
    descriptor: int,
    offset: int,
    limit: int,
    cancelled: Event,
) -> tuple[str, int]:
    """按 UTF-8 字符分页，多保留一个字符以判断是否还有下一页。

    返回的长度是扫描到的位置，不保证是全文长度；线程通过 Event 协作取消。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    parts: list[str] = []
    length = 0
    page_end = offset + limit + 1
    while True:
        if cancelled.is_set():
            raise asyncio.CancelledError
        chunk = os.read(descriptor, 8_192)
        decoded = decoder.decode(chunk, final=not chunk)
        if "\x00" in decoded:
            raise ValueError("NUL is not text")
        decoded_end = length + len(decoded)
        if length < page_end and decoded_end > offset:
            start = max(0, offset - length)
            end = min(len(decoded), page_end - length)
            parts.append(decoded[start:end])
        length = decoded_end
        if length >= page_end or not chunk:
            return "".join(parts), length
