"""Evolution Agent 请求级 Train Evidence 工具。"""

import json
import re
from threading import RLock

from agentsystem.common.errors import ToolError
from agentsystem.common.models import JsonValue
from agentsystem.tool.models import ToolOutput

from evolution.execution import format_evaluation_json
from evolution.improver import ImprovementRequest
from evolution.models import CaseEvaluation

_TRACELOG_HINT = (
    "用 read_case_tracelog 读取轨迹，按 next_offset 续读。"
    "展开截断长行时，指定该行 offset、limit=1、truncate=false。"
)
_LINE_CHAR_LIMIT = 2000
_PAGE_CHAR_BUDGET = 20_000


class EvidenceTools:
    """只投影当前 ImprovementRequest 中的 Train 证据。"""

    def __init__(self) -> None:
        """初始化尚未绑定请求的证据缓存与同步锁。"""
        self._request: ImprovementRequest | None = None
        self._lines: dict[str, tuple[str, ...]] = {}
        self._lock = RLock()

    def bind(self, request: ImprovementRequest) -> None:
        """绑定当前唯一可见的改进请求。"""
        with self._lock:
            self._request = request
            self._lines = {}

    def unbind(self) -> None:
        """清除本轮请求，避免证据跨轮残留。"""
        with self._lock:
            self._request = None
            self._lines = {}

    def read_case_evidence(self, case_id: str) -> ToolOutput:
        """返回 Case 评分证据，以行数和读取指引代替轨迹正文。"""
        case, lines = self._case_view(case_id)
        payload = case.model_dump(mode="json", exclude={"tracelog"})
        if payload["failure_kind"] is None:
            # 无类别信息时不占位，保持证据视图最小。
            del payload["failure_kind"]
        payload["tracelog"] = {"total_lines": len(lines), "hint": _TRACELOG_HINT}
        return _output(payload)

    def read_case_tracelog(
        self,
        case_id: str,
        offset: int = 0,
        limit: int | None = 500,
        truncate: bool = True,
    ) -> ToolOutput:
        """默认读取 500 行；limit=None 按字符预算分页，允许展开完整行。"""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ToolError(code="invalid_offset", message="offset 必须是非负整数")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ToolError(code="invalid_limit", message="limit 必须是正整数")
        if not isinstance(truncate, bool):
            raise ToolError(code="invalid_arguments", message="truncate 必须是布尔值")
        _, lines = self._case_view(case_id)
        end = len(lines) if limit is None else min(offset + limit, len(lines))
        parts = [f"--- {case_id}/tracelog total_lines={len(lines)}"]
        body_chars = 0
        next_offset = offset
        for index in range(offset, end):
            line = lines[index]
            shown = line
            if truncate and len(line) > _LINE_CHAR_LIMIT:
                shown = (
                    line[:_LINE_CHAR_LIMIT]
                    + f" [read_case_tracelog 展示截断：原文共 {len(line)} 字符；"
                    f"完整读取：offset={index}, limit=1, truncate=false]"
                )
            rendered = f"{index + 1}\t{shown}"
            size = len(rendered) + 1
            # 首行即使超预算也完整返回，保证全文展开和续读都能前进。
            if limit is None and body_chars and body_chars + size > _PAGE_CHAR_BUDGET:
                break
            parts.append(rendered)
            body_chars += size
            next_offset = index + 1
        eof = str(next_offset >= len(lines)).lower()
        parts.append(f"--- next_offset={next_offset} eof={eof}")
        return ToolOutput(content="\n".join(parts))

    def _case_view(self, case_id: str) -> tuple[CaseEvaluation, tuple[str, ...]]:
        """读取当前请求的 Case 并缓存轨迹行；请求切换后拒绝旧视图。"""
        with self._lock:
            request = self._bound_request()
            if not isinstance(case_id, str) or not case_id:
                raise ToolError(code="invalid_arguments", message="case_id 必须非空")
            case = next(
                (case for case in request.train_evaluation.cases if case.id == case_id),
                None,
            )
            if case is None:
                raise ToolError(
                    code="case_not_found", message=f"Train Case 不存在：{case_id}"
                )
            cache = self._lines
            if case_id in cache:
                return case, cache[case_id]
        # 大视图在线程中生成，不持锁阻塞事件循环的 bind/unbind。
        lines = _trace_lines(case.tracelog)
        with self._lock:
            if self._lines is not cache:
                raise ToolError(
                    code="request_not_bound", message="证据请求已结束或切换"
                )
            return case, cache.setdefault(case_id, lines)

    def _bound_request(self) -> ImprovementRequest:
        """取得当前绑定请求；未绑定时抛出 ToolError。"""
        if self._request is None:
            raise ToolError(code="request_not_bound", message="尚未绑定改进请求")
        return self._request


def _output(value: object) -> ToolOutput:
    """将证据对象编码为保留中文的 JSON 工具响应。"""
    return ToolOutput(content=json.dumps(value, ensure_ascii=False))


def _trace_lines(value: JsonValue) -> tuple[str, ...]:
    """将轨迹转为可分页文本行，兼容文本及结构化 JSON。"""
    text = value if isinstance(value, str) else format_evaluation_json(value)
    if not text:
        return ()
    lines = re.split(r"\r\n|\r|\n", text)
    if lines[-1] == "":
        lines.pop()
    return tuple(lines)


__all__ = ["EvidenceTools"]
