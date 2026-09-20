"""专家系统 · 红线护栏（engine/expert/base）。

两专家共用的护栏，保证专家用例**只增不加、不臆造、不静默**：

1. **不臆造（validate_area_hits）**：专家测试点的 `area` 必须命中真实锚点——
   URL 通道命中已抓取 elements / discovered_routes / xhr；代码通道命中真实 fp_id。
   未命中即进 `rejected` 并公示原因，绝不落到产物。
2. **不静默（ExpertResult.notes）**：未启用 / 模型缺失 / 视觉模型未配 → 返回空 added
   并显式 notes（延续 F10b），绝不假装已生效。
3. **优雅降级（graceful）**：LLM 失败 → 调用方捕获后返回空 added + notes，不影响规则基线。

只依赖 core，不感知 service / cli。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExpertResult:
    """专家阶段统一结果（added / rejected / notes / 覆盖缺口）。"""

    added: list[Any] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    def add_note(self, note: str) -> None:
        if note and note not in self.notes:
            self.notes.append(note)

    def reject(self, item: Any, reason: str) -> None:
        self.rejected.append({"reason": reason, "item": item})


def validate_area_hits(
    area: str,
    anchors: set[str],
    *,
    normalize: bool = True,
) -> str | None:
    """护栏：返回拒绝原因字符串；通过返回 None。

    `area`：专家测试点声明的锚点（页面路径 / 元素选择器 / 接口路径 / fp_id）。
    `anchors`：本次探索/分析真实存在的锚点集合。
    `normalize`：是否对双方做归一化（去空白、去前缀 `runtime:`、统一小写）再比对；
     元素选择器大小写敏感，调用方可传 False。
    """
    a = (area or "").strip()
    if not a:
        return "area 为空（无依据，疑似臆造）"
    if normalize:
        norm = {_norm(x) for x in anchors}
        if _norm(a) not in norm:
            return f"area 未命中任何真实锚点（疑似臆造）：{a!r}"
    elif a not in anchors:
        return f"area 未命中任何真实锚点（疑似臆造）：{a!r}"
    return None


def _norm(value: str) -> str:
    v = value.strip()
    if v.startswith("runtime:"):
        v = v[len("runtime:") :]
    return v.lower()


def build_anchor_pool(
    *,
    element_selectors: list[str] | None = None,
    routes: list[str] | None = None,
    xhr_paths: list[str] | None = None,
    fp_ids: list[str] | None = None,
) -> set[str]:
    """把 URL 通道（elements/routes/xhr）或代码通道（fp_ids）的真实锚点合并成一个集合。

    归一化在 `validate_area_hits` 内完成；此处只做去重合并。
    """
    pool: set[str] = set()
    pool.update(element_selectors or [])
    pool.update(routes or [])
    pool.update(xhr_paths or [])
    pool.update(fp_ids or [])
    return pool
