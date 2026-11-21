"""引擎 · T2 影响面分析（impact）。

回答的是「改了这个函数，**还有哪些入口会因为这次改动而需要回归**」——函数级收窄只保留
「改动本体」，被改函数的调用方一律按「未变更」丢掉，于是「改 A 崩 B」这类回归完全裸奔。

分析口径（与 `incremental_narrow` 共用同一份 AST 调用图，不做第二套解析）：

- **L1 直接调用**：被改函数在**同文件**反向调用图上 ≤`max_hops` 跳可达的入口
  （入口 = 带路由装饰器的函数 / lifespan / on_event）。跨文件一律不猜——import 解析
  误报率高，宁可少报也不造无来源用例。
- **L2 数据契约**：本次 hunk 新增/修改的**字符串字面量**（文案、字段名）如果被某个入口
  引用，该入口判为「数据契约受影响」而不是「主流程回归」——前者验数据形状兼容，
  后者验主流程可用，两者的断言写法不同。

防膨胀（历史教训：一个私有函数 20 个调用点 → 20 条几乎一样的用例）：
- 按**入口聚合**：同一入口的多个调用点合并成 1 条，断言列多个场景；
- `max_per_func`：一个被改函数最多影响 `max_per_func` 个入口；
- `max_total`：整次最多 `max_total` 条影响用例，超出的进 `truncated` 如实上报。

不产点的情形（一律不静默编造）：无可达入口、入口不是路由/lifespan、拿不到入口的
功能点锚点（归属失败时用例无法追溯到功能点，落库会成孤儿）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from core.contracts import FunctionalPoint, TestPoint, tp_id_of
from core.enums import Dimension, Tag, TPType, VerifyLayer
from engine import fp_extract
from engine.incremental_narrow import (
    _entry_ancestors,
    _innermost_func,
    _overlaps,
    _parse_line,
    file_call_graph,
)


# 影响类型：直接调用 vs 数据契约
KIND_DIRECT = "direct"
KIND_DATA = "data"

# 默认最大跳数：1 跳是主口径；允许 2 跳覆盖「私有函数被另一层私有函数包一层」的常见写法
IMPACT_MAX_HOPS = 2
DEFAULT_MAX_PER_FUNC = 3
DEFAULT_MAX_TOTAL = 15

# 从改动行里提取字符串字面量（文案/字段名）；太短的噪声（空串、"a"）不参与判定
_STR_LITERAL_RE = re.compile(r'"([^"\n]{2,60})"|\'([^\'\n]{2,60})\'')


@dataclass
class ImpactItem:
    """一条影响面结论：某个入口受某次改动影响，需要回归。"""

    entry: str  # 入口函数限定名
    method: str  # HTTP 方法（入口若为 lifespan 则为空）
    path: str  # 入口路由路径（用于真实探测）
    kind: str = KIND_DIRECT
    hops: int = 1
    anchor_fp_id: str = ""  # 追溯锚点：本次改动归属到的功能点（保证落库不产生孤儿用例）
    module: str = ""
    source: str = ""  # 被改文件（相对路径）
    changed_funcs: list[str] = field(default_factory=list)
    literals: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def dimension(self) -> str:
        return (
            Dimension.IMPACT_DATA.value if self.kind == KIND_DATA else Dimension.IMPACT_DIRECT.value
        )

    @property
    def tag(self) -> str:
        return Tag.IMPACT_DATA.value if self.kind == KIND_DATA else Tag.IMPACT_DIRECT.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "method": self.method,
            "path": self.path,
            "kind": self.kind,
            "hops": self.hops,
            "anchor_fp_id": self.anchor_fp_id,
            "module": self.module,
            "source": self.source,
            "changed_funcs": self.changed_funcs,
            "literals": self.literals,
            "evidence": self.evidence,
            "dimension": self.dimension,
            "tag": self.tag,
        }


@dataclass
class ImpactPlan:
    """影响面计划（纯数据，供测试点转换与报告消费）。"""

    items: list[ImpactItem] = field(default_factory=list)
    truncated: int = 0
    reachable_entries: int = 0
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """影响入口覆盖率：已产出的入口 / 可达入口（无入口时记 1.0，表示无可覆盖项）。"""
        if self.reachable_entries <= 0:
            return 1.0
        return round(len(self.items) / self.reachable_entries, 3)


def _changed_funcs(graph: Any, ranges: list[tuple[int, int]]) -> list[str]:
    """本次 hunk 落在哪些函数里（最内层，排除入口自身——入口是「本体」不是「影响面」）。"""
    out: list[str] = []
    seen: set[str] = set()
    for start, length in ranges:
        end = start + max(int(length), 1) - 1
        for qname, node in graph.defs.items():
            fn_start = int(getattr(node, "lineno", 0) or 0)
            fn_end = int(getattr(node, "end_lineno", fn_start) or fn_start)
            if fn_start <= 0 or not _overlaps(start, end, fn_start, fn_end):
                continue
            inner = _innermost_func(graph, start) or qname
            if inner in graph.entries or inner in seen:
                continue
            seen.add(inner)
            out.append(inner)
    return out


def _is_meaningful_literal(value: str) -> bool:
    """过滤 diff 行里的噪声字面量（如 ``", "``）：至少 3 个字符且含字母/数字/中文。"""
    text = value.strip()
    if len(text) < 3:
        return False
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text)


def _changed_literals(sf: Any, ranges: list[tuple[int, int]]) -> list[str]:
    """改动行里出现的字符串字面量（文案/字段名），用于 L2 数据契约判定。"""
    lines = sf.text.splitlines() if hasattr(sf, "text") else []
    found: list[str] = []
    for start, length in ranges:
        for line in lines[start - 1 : start - 1 + max(int(length), 1)]:
            for m in _STR_LITERAL_RE.finditer(line):
                value = m.group(1) or m.group(2)
                if value and _is_meaningful_literal(value) and value not in found:
                    found.append(value)
    return found


def _entry_route(graph: Any, prefix: str, entry: str) -> tuple[str, str] | None:
    """入口函数的路由（方法, 完整路径）；lifespan 类入口无 HTTP 语义，返回 None。"""
    node = graph.defs.get(entry)
    route = fp_extract._first_route(node) if node is not None else None
    if not route:
        return None
    method, path = route
    full = (prefix + path) if prefix and path.startswith("/") else (path or prefix)
    return method, (full or "/")


def _entry_uses_literal(graph: Any, entry: str, literals: list[str]) -> list[str]:
    """入口函数体里引用了哪些改动字面量（非空即判为数据契约受影响）。"""
    node = graph.defs.get(entry)
    if node is None or not literals:
        return []
    body = getattr(node, "body", [])
    used: list[str] = []
    for lit in literals:
        for stmt in body:
            if lit in _node_source(stmt):
                used.append(lit)
                break
    return used


def _node_source(node: Any) -> str:
    """把 AST 节点还原成源码片段（仅用于「是否引用了该字面量」的判定）。"""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - unparse 失败按「未引用」处理，不阻断分析
        return ""


def _anchor_fp(
    index: dict[tuple[str, int], FunctionalPoint],
    rel: str,
    graph: Any,
    entries: list[tuple[int, str]],
) -> FunctionalPoint | None:
    """在本函数的可达入口里找「已有功能点」的那个作追溯锚点（最近跳优先）。"""
    for _hops, entry in entries:
        fp = index.get((rel, graph.lines.get(entry, 0)))
        if fp is not None:
            return fp
    return None


def _collect_for_file(
    rel: str,
    sf: Any,
    ranges: list[tuple[int, int]],
    index: dict[tuple[str, int], FunctionalPoint],
    max_hops: int,
) -> tuple[list[tuple[int, str, str, str, str, list[str], str]], int]:
    """单个文件的影响候选：``[(跳数, 入口, 方法, 路径, 改动函数, 命中字面量, 锚点fp)]``。"""
    graph = file_call_graph(sf)
    if graph is None:
        return [], 0
    prefix = fp_extract._router_prefix(sf.tree())
    literals = _changed_literals(sf, ranges)
    out: list[tuple[int, str, str, str, str, list[str], str]] = []
    reachable = 0
    for func in _changed_funcs(graph, ranges):
        entries = _entry_ancestors(graph, func, max_hops)
        # 已经有功能点（= 本体用例已覆盖）的入口不算「需要影响覆盖」：避免与 T1 归属重复
        needed = [(h, e) for h, e in entries if index.get((rel, graph.lines.get(e, 0))) is None]
        reachable += len(needed)
        if not needed:
            continue
        anchor = _anchor_fp(index, rel, graph, entries)
        if anchor is None:
            continue  # 归属失败：用例无法追溯到功能点，宁可不产
        for hops, entry in needed:
            route = _entry_route(graph, prefix, entry)
            if route is None:
                continue
            used = _entry_uses_literal(graph, entry, literals)
            out.append((hops, entry, route[0], route[1], func, used, anchor.fp_id))
    return out, reachable


def _aggregate_items(
    candidates: list[tuple[int, str, str, str, str, list[str], str, str]],
    max_per_func: int,
) -> tuple[dict[str, ImpactItem], set[str]]:
    """按入口聚合候选（同一入口的多个调用点合并成 1 条），并执行单函数预算。"""
    per_func: dict[str, int] = {}
    items: dict[str, ImpactItem] = {}
    seen_entries: set[str] = set()
    for hops, entry, method, path, func, used, anchor_id, rel in sorted(
        candidates, key=lambda c: (c[0], c[2], c[3])
    ):
        seen_entries.add(entry)
        if entry not in items and per_func.get(func, 0) >= max(1, max_per_func):
            continue
        item = items.get(entry)
        if item is None:
            item = ImpactItem(
                entry=entry,
                method=method,
                path=path,
                hops=hops,
                anchor_fp_id=anchor_id,
                module=fp_extract.module_of(rel),
                source=rel,
            )
            items[entry] = item
        per_func[func] = per_func.get(func, 0) + 1
        item.changed_funcs.append(func)
        item.evidence.append(f"{rel}: {func} 被 {entry} 调用（{hops} 跳）")
        if used:
            item.kind = KIND_DATA
            for lit in used:
                if lit not in item.literals:
                    item.literals.append(lit)
    return items, seen_entries


def build_impact_plan(  # noqa: PLR0913 - 预算与跳数都是显式策略参数，包成对象反而更难读
    files: dict[str, Any],
    hunks: dict[str, list[tuple[int, int]]],
    kept_fps: list[FunctionalPoint],
    *,
    max_per_func: int = DEFAULT_MAX_PER_FUNC,
    max_total: int = DEFAULT_MAX_TOTAL,
    max_hops: int = IMPACT_MAX_HOPS,
) -> ImpactPlan:
    """构造影响面计划：按入口聚合、受两条预算约束。"""
    index: dict[tuple[str, int], FunctionalPoint] = {}
    for fp in kept_fps:
        parsed = _parse_line(fp)
        if parsed:
            index.setdefault(parsed, fp)

    plan = ImpactPlan()
    candidates: list[tuple[int, str, str, str, str, list[str], str, str]] = []
    for rel, ranges in hunks.items():
        sf = files.get(rel) or files.get(rel.replace("\\", "/"))
        if sf is None:
            continue
        found, reachable = _collect_for_file(rel, sf, ranges, index, max_hops)
        plan.reachable_entries += reachable
        candidates.extend((*item, rel) for item in found)

    items, seen_entries = _aggregate_items(candidates, max_per_func)
    ordered = sorted(items.values(), key=lambda i: (i.hops, -len(i.changed_funcs), i.path))
    plan.items = ordered[: max(1, max_total)]
    plan.truncated = max(0, len(seen_entries) - len(plan.items))
    plan.stats = {
        "impact_items": len(plan.items),
        "impact_direct": sum(1 for i in plan.items if i.kind == KIND_DIRECT),
        "impact_data": sum(1 for i in plan.items if i.kind == KIND_DATA),
        "impact_truncated": plan.truncated,
        "impact_reachable_entries": plan.reachable_entries,
    }
    return plan


def impact_test_points(
    plan: ImpactPlan,
    *,
    review_status: str = "approved",
) -> list[TestPoint]:
    """影响面计划 → 测试点（category=正常：验证的是「主流程未回归」）。"""
    out: list[TestPoint] = []
    for item in plan.items:
        if not item.anchor_fp_id:
            continue
        dimension = item.dimension
        funcs = "、".join(dict.fromkeys(item.changed_funcs))
        expect = _impact_expect(item, funcs)
        out.append(
            TestPoint(
                tp_id=tp_id_of(
                    item.anchor_fp_id,
                    TPType.NORMAL.value,
                    area=item.path,
                    method=item.method,
                    dimension=dimension,
                ),
                fp_contract_id=item.anchor_fp_id,
                category=TPType.NORMAL.value,
                module=item.module,
                title=f"[正常] {item.module} · {item.method} {item.path} · {dimension}",
                semantic=f"影响面回归：{funcs} 改动后，{item.method} {item.path} 需复测",
                source=item.source,
                method=item.method,
                area=item.path,
                expect=expect,
                dimension=dimension,
                tag=item.tag,
                review_status=review_status,
                verify_layer=VerifyLayer.INTERFACE.value,
                evidence=list(item.evidence),
                origin="impact",
                unverified=False,
            )
        )
    return out


def _impact_expect(item: ImpactItem, funcs: str) -> str:
    """影响面断言：固定句式「因 X 改动，验证 Y 未回归」，保证可判定。"""
    where = f"{item.source}"
    if item.kind == KIND_DATA:
        lits = "、".join(item.literals[:3]) or "输出字段/文案"
        return (
            f"因 {funcs}（{where}）改动了 {lits}，验证 {item.method} {item.path} 未回归："
            "返回 2xx，字段齐备且取值与改动后语义一致（无空值、无错位、无旧文案残留）"
        )
    return (
        f"因 {funcs}（{where}）改动，验证 {item.method} {item.path} 未回归："
        "返回 2xx，主流程仍可走通，响应结构与改动前一致（关键字段不缺失、不新增必填项）"
    )
