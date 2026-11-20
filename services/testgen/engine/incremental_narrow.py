"""引擎 · 增量收窄（incremental_narrow）。

增量模式下把功能点收窄到「本次真正改动的函数」，避免一个文件只改 2 个函数、
却把整文件 89 个功能点全部展开成测试点/用例（历史上 843 条的膨胀根因）。

设计要点：
- **后端 Python 功能点（API / BUSINESS）**：每个功能点的 ``description`` 形如
  ``"func() @ path/to/file.py:1234"``，携带函数定义行号。用 AST 反查该行号所在
  函数的**完整范围** ``[lineno, end_lineno]``，判断它是否与任一 diff hunk 的
  新文件行区间 ``[start, start+length]`` 有交集：
    - 有交集 → 函数被本次改动触及 → 保留；
    - 无交集 → 未变更函数 → 过滤（不产出测试点/用例）。
  用「完整函数范围」而不是「定义行号」判断，是因为改动常发生在函数体中段
  （如 ``get_thread`` 的 def 行不在 hunk、变更在 6110 行附近），只比定义行会误删。
- **前端 / 无行号功能点（PAGE / UI / COMPONENT）**：``description`` 只有文件名，
  拿不到函数行号 → 按文件级判断（文件在变更集即「更新」），保留不删除；但打上
  「维度收敛」标记，展开阶段只出核心维度（见 tp_expand 的 `narrow_fp_ids`），
  避免限流/降级/令牌过期等泛化模板在无行号文件上膨胀。
- **新增 / 删除文件**：整文件都是新增/删除，全部函数都算变更 → 保留该功能点。
- **全量模式 / 无 hunk 信息**：直接返回全部功能点（零改动），保证向后兼容。
- **改动归属（T1，默认开）**：hunk 落在「没有功能点的函数」上（私有函数被
  `fp_extract` 跳过、或辅助函数被 F6 调用图吞并）时，函数级收窄会把这次改动整体丢掉
  ——表现为「后端明明改了，却一条用例都没有」。此补救沿**同文件反向调用图**向上找
  最近的路由入口（含 lifespan/on_event），把该入口的功能点补回保留集，
  证据写进 `attributed_evidence`。按入口去重、默认只归属 1 个代表入口，避免一个
  私有函数被 20 处调用就炸出 20 个入口的用例（影响面扩散是 `engine/impact.py` 的职责）。

收窄只影响「哪些功能点进入展开」，不改功能点本身、不改打标、不改持久化契约。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from core.contracts import FunctionalPoint
from core.enums import FType


# description 里的行号证据："func() @ path/to/file.py:1234"
_LINE_RE = re.compile(r"^(?P<name>.+?)\s+@\s+(?P<path>.+):(?P<line>\d+)$")
# unified diff 的 hunk 头："@@ -old,len +new,len @@" → 取新文件行号 (start, len)
_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# 后端可定位到函数的功能点类型（description 带行号）
_LOCATABLE_FTYPES = {FType.API.value, FType.BUSINESS.value}

# T1 改动归属：向上找入口的最大跳数（私有函数 → … → 路由；再深多半是工具函数链）
_ATTR_MAX_DEPTH = 4
# 生命周期类装饰器后缀：这些函数也是「外部触发入口」，改动同样要能从它这里被验证
_LIFECYCLE_ATTRS = ("lifespan", "on_event")


@dataclass
class NarrowResult:
    """收窄结果。"""

    kept: list[FunctionalPoint] = field(default_factory=list)
    dropped_fps: list[str] = field(default_factory=list)  # 被过滤掉的功能点 id（未变更函数）
    narrow_fp_ids: list[str] = field(default_factory=list)  # 需收敛维度的功能点 id（前端/无行号）
    stats: dict[str, int] = field(default_factory=dict)
    # T1：归属补救的逐条证据（fp_id / 入口 / 途经函数 / hunk 行），供报告与追溯使用
    attributed_evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.stats)


def _parse_line(fp: FunctionalPoint) -> tuple[str, int] | None:
    """从功能点 description 解析出 (相对路径, 定义行号)；无证据返回 None。"""
    if fp.ftype not in _LOCATABLE_FTYPES:
        return None
    m = _LINE_RE.match((fp.description or "").strip())
    if not m:
        return None
    try:
        return m.group("path").replace("\\", "/"), int(m.group("line"))
    except (ValueError, TypeError):
        return None


def _function_ranges(sf: Any) -> list[tuple[int, int]]:
    """返回源文件里所有函数定义的 (起始行, 结束行) 范围（含嵌套方法）。"""
    tree = sf.tree() if hasattr(sf, "tree") else None
    if tree is None:
        return []
    out: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = int(getattr(node, "lineno", 0) or 0)
            end = int(getattr(node, "end_lineno", 0) or start)
            if start > 0:
                out.append((start, max(end, start)))
    return out


def _line_in_changed_function(
    rel: str, lineno: int, hunks: dict[str, list[tuple[int, int]]], files: dict[str, Any]
) -> bool | None:
    """判断功能点定义行所在函数是否被改动。

    返回 ``True``=函数被改动 / ``False``=未改动 / ``None``=无法判定（缺 AST 或 hunk）。
    """
    ranges = hunks.get(rel.replace("\\", "/"))
    if not ranges:
        return None  # 该文件没有 hunk（可能整文件新增，或拿不到行号）
    sf = files.get(rel) or files.get(rel.replace("\\", "/"))
    if sf is None:
        return None
    fn_ranges = _function_ranges(sf)
    if not fn_ranges:
        return None
    # 功能点定义行落在哪个函数体内（取包含该行的最小范围）
    containing = [r for r in fn_ranges if r[0] <= lineno <= r[1]]
    if not containing:
        return None
    fn_start, fn_end = min(containing, key=lambda r: r[1] - r[0])
    # 函数体范围与任一 hunk 是否相交
    for h_start, h_len in ranges:
        h_end = h_start + max(int(h_len), 1) - 1
        if fn_start <= h_end and h_start <= fn_end:
            return True
    return False


# ============================================================================
# T1：改动归属（hunk 落在无功能点的函数 → 补回最近入口的功能点）
# ============================================================================
@dataclass
class _FileGraph:
    """同文件调用图：只用于把「没有功能点的改动」向上溯源到入口。"""

    defs: dict[str, Any] = field(default_factory=dict)
    callers: dict[str, set[str]] = field(default_factory=dict)
    entries: set[str] = field(default_factory=set)
    lines: dict[str, int] = field(default_factory=dict)


def _decorator_attr(dec: Any) -> str:
    """取装饰器的最末属性名：`@router.get(...)` → `get`，`@lifespan` → `lifespan`。"""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _is_lifecycle_entry(node: Any) -> bool:
    """该函数是否由框架生命周期触发（`@app.on_event` / `@asynccontextmanager lifespan`）。"""
    return any(
        _decorator_attr(dec) in _LIFECYCLE_ATTRS
        for dec in getattr(node, "decorator_list", []) or []
    )


def _file_graph(sf: Any) -> _FileGraph | None:
    """构造同文件调用图；复用 fp_extract 已有的 AST 工具，保证与功能点提取口径一致。"""
    tree = sf.tree() if hasattr(sf, "tree") else None
    if tree is None:
        return None
    from engine import fp_extract as fx  # 延迟导入：避免模块初始化期互相牵制

    qualnames = fx._qualified_names(tree)
    defs, by_name, routes = fx._function_defs(tree, qualnames)
    entries = set(routes)
    entries.update(q for q, node in defs.items() if _is_lifecycle_entry(node))
    return _FileGraph(
        defs=defs,
        callers=fx._callers_in_file(defs, by_name),
        entries=entries,
        lines={q: int(getattr(n, "lineno", 0) or 0) for q, n in defs.items()},
    )


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def file_call_graph(sf: Any) -> _FileGraph | None:
    """对外暴露同文件调用图（影响面分析 T2 复用同一 AST 口径，避免两套解析）。"""
    return _file_graph(sf)


def _innermost_func(graph: _FileGraph, line: int) -> str | None:
    """返回包含该行的**最内层**函数限定名（嵌套函数优先于外层）。"""
    hits = []
    for qname, node in graph.defs.items():
        start = int(getattr(node, "lineno", 0) or 0)
        end = int(getattr(node, "end_lineno", start) or start)
        if start > 0 and start <= line <= end:
            hits.append((end - start, start, qname))
    return min(hits)[2] if hits else None


def _fp_spans(fps: list[FunctionalPoint], rel: str, graph: _FileGraph) -> list[tuple[int, int]]:
    """该文件里**已有功能点**覆盖的函数行区间（用于判断某个 hunk 是否已被功能点接住）。"""
    out: list[tuple[int, int]] = []
    for fp in fps:
        parsed = _parse_line(fp)
        if not parsed or parsed[0] != rel:
            continue
        qname = _innermost_func(graph, parsed[1])
        node = graph.defs.get(qname) if qname else None
        if node is None:
            continue
        start = int(getattr(node, "lineno", 0) or 0)
        out.append((start, int(getattr(node, "end_lineno", start) or start)))
    return out


def _entry_ancestors(graph: _FileGraph, qname: str, max_depth: int) -> list[tuple[int, str]]:
    """沿反向调用图向上找入口，返回 ``[(跳数, 入口限定名)]``（同层按源码顺序稳定）。"""
    found: list[tuple[int, str]] = []
    seen = {qname}
    frontier = [qname]
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt: list[str] = []
        for cur in frontier:
            for caller in sorted(graph.callers.get(cur, ())):
                if caller in seen:
                    continue
                seen.add(caller)
                if caller in graph.entries:
                    found.append((depth, caller))
                nxt.append(caller)
        frontier = nxt
    return found


@dataclass
class AttributionOptions:
    """T1 改动归属的开关与上限（由 `core.config` 注入，便于灰度与回滚）。"""

    enabled: bool = False
    max_entries: int = 1


def _fp_index(fps: list[FunctionalPoint]) -> dict[tuple[str, int], FunctionalPoint]:
    """``(相对路径, 定义行) → 功能点``：用于按入口函数的定义行找回它的功能点。"""
    index: dict[tuple[str, int], FunctionalPoint] = {}
    for fp in fps:
        parsed = _parse_line(fp)
        if parsed:
            index.setdefault(parsed, fp)
    return index


def _uncovered_candidates(
    fps: list[FunctionalPoint], files: dict[str, Any], hunks: dict[str, list[tuple[int, int]]]
) -> tuple[list[tuple[int, int, str, int, str, str]], int]:
    """收集「hunk 未被任何功能点接住」的归属候选。

    返回 ``(候选列表, 未覆盖 hunk 数)``；候选元素为
    ``(跳数, 入口定义行, 文件, hunk 行, 入口限定名, 途经函数)``，已按跳数/源码顺序排序。
    """
    candidates: list[tuple[int, int, str, int, str, str]] = []
    uncovered = 0
    for rel, ranges in hunks.items():
        sf = files.get(rel) or files.get(rel.replace("\\", "/"))
        graph = _file_graph(sf) if sf is not None else None
        if graph is None:
            continue
        spans = _fp_spans(fps, rel, graph)
        for h_start, h_len in ranges:
            h_end = h_start + max(int(h_len), 1) - 1
            if any(_overlaps(h_start, h_end, s, e) for s, e in spans):
                continue  # 该 hunk 已被现有功能点接住，不属于「改动丢失」
            uncovered += 1
            inner = _innermost_func(graph, h_start)
            if not inner:
                continue
            for depth, entry in _entry_ancestors(graph, inner, _ATTR_MAX_DEPTH):
                candidates.append((depth, graph.lines.get(entry, 0), rel, h_start, entry, inner))
    candidates.sort()
    return candidates, uncovered


def _materialize_attributions(
    candidates: list[tuple[int, int, str, int, str, str]],
    index: dict[tuple[str, int], FunctionalPoint],
    kept_ids: set[str],
    max_entries: int,
) -> tuple[list[FunctionalPoint], list[dict[str, Any]]]:
    """按入口去重、按上限截断，把候选落成「补回的功能点 + 归属证据」。"""
    added: list[FunctionalPoint] = []
    evidence: list[dict[str, Any]] = []
    used: set[str] = set()
    for depth, line, rel, h_start, entry, inner in candidates:
        if len(added) >= max(1, max_entries):
            break
        if entry in used:
            continue
        used.add(entry)
        fp = index.get((rel, line))
        if fp is None or fp.fp_id in kept_ids:
            continue
        added.append(fp)
        kept_ids.add(fp.fp_id)
        evidence.append(
            {
                "fp_id": fp.fp_id,
                "entry": entry,
                "via": inner,
                "file_path": rel,
                "hunk_line": h_start,
                "hops": depth,
                "reason": "hunk 落在无功能点的函数（私有/被吞并），按同文件调用图归属到最近入口",
            }
        )
    return added, evidence


def attribute_changes(
    fps: list[FunctionalPoint],
    kept: list[FunctionalPoint],
    files: dict[str, Any],
    hunks: dict[str, list[tuple[int, int]]],
    options: AttributionOptions | None = None,
) -> tuple[list[FunctionalPoint], list[dict[str, Any]], int]:
    """为「hunk 落在没有功能点的函数」补回最近入口的功能点。

    返回 ``(新增功能点, 证据, 未覆盖 hunk 数)``。只按入口去重、只取最近一跳入口，
    上限 ``options.max_entries``：一个私有函数被 20 处调用时不至于炸出 20 个入口的用例。
    """
    opts = options or AttributionOptions()
    if not opts.enabled or not hunks:
        return [], [], 0
    candidates, uncovered = _uncovered_candidates(fps, files, hunks)
    added, evidence = _materialize_attributions(
        candidates, _fp_index(fps), {fp.fp_id for fp in kept}, opts.max_entries
    )
    return added, evidence, uncovered


def hunks_from_compare_diffs(diffs: list[dict[str, Any]]) -> dict[str, list[tuple[int, int]]]:
    """从 Codeup compare 返回的 diffs 解析出 ``{newPath: [(新文件起始行, 行数)]}``。

    Codeup ``compare`` 的每个 diff 带 ``diff`` 字段（unified diff 文本）。用它而不是
    本地 git diff，是因为沙箱 sidecar 进程里 git diff 可能返回空（环境差异），而
    scan 阶段的 Codeup HTTP compare 已验证稳定可用。
    """
    out: dict[str, list[tuple[int, int]]] = {}
    for d in diffs or []:
        if not isinstance(d, dict) or d.get("deletedFile"):
            continue
        path = d.get("newPath") or d.get("bPath") or d.get("path") or ""
        if not path:
            continue
        path = str(path).replace("\\", "/")
        ranges: list[tuple[int, int]] = []
        for line in (d.get("diff") or "").splitlines():
            m = _DIFF_HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                length = int(m.group(2)) if m.group(2) is not None else 1
                ranges.append((start, max(length, 1)))
        if ranges:
            out.setdefault(path, []).extend(ranges)
    return out


def narrow_to_changed_functions(
    fps: list[FunctionalPoint],
    files: dict[str, Any],
    hunks: dict[str, list[tuple[int, int]]],
    *,
    changed_files: set[str] | None = None,
    attribution: AttributionOptions | None = None,
) -> NarrowResult:
    """把功能点收窄到变更函数；前端/无行号功能点保留并标记收敛维度。

    参数：
        fps: 候选功能点（已按 selected_paths 过滤到勾选文件）。
        files: 扫描到的源文件字典（rel → SourceFile），用于 AST 反查函数范围。
        hunks: ``{rel: [(新文件起始行, 行数), ...]}``，来自 ``diff_tag.compute_diff_hunks``。
        changed_files: 变更文件集合（仅用于备注统计，判定以 hunks 为准）。
        attribution: T1 改动归属选项（hunk 落在无功能点函数时补回最近入口）。

    向后兼容：``hunks`` 为空时**不删任何功能点**，只把前端/无行号功能点标记为收敛。
    """
    result = NarrowResult()
    changed_files = changed_files or set()
    locatable = 0
    frontend_marked = 0

    for fp in fps:
        parsed = _parse_line(fp)
        if parsed is None:
            # 前端/无行号：文件级判断——文件在变更集里才算「更新」，保留但收敛维度
            result.kept.append(fp)
            result.narrow_fp_ids.append(fp.fp_id)
            frontend_marked += 1
            continue
        locatable += 1
        rel, lineno = parsed
        if not hunks:
            # 拿不到行号信息：不收窄后端，保守保留（仅前端走 B 兜底）
            result.kept.append(fp)
            continue
        verdict = _line_in_changed_function(rel, lineno, hunks, files)
        if verdict is False:
            result.dropped_fps.append(fp.fp_id)
        else:
            # True=改动过保留；None=无法判定，保守保留
            result.kept.append(fp)

    attributed: list[FunctionalPoint] = []
    uncovered = 0
    if attribution is not None and attribution.enabled and hunks:
        attributed, evidence, uncovered = attribute_changes(
            fps, result.kept, files, hunks, attribution
        )
        result.kept.extend(attributed)
        result.attributed_evidence = evidence
        # 补回的功能点原本在 dropped 里（自己的函数范围没压中 hunk），统计口径要一起改
        back = {fp.fp_id for fp in attributed}
        if back:
            result.dropped_fps = [i for i in result.dropped_fps if i not in back]

    result.stats = {
        "fp_before": len(fps),
        "fp_after": len(result.kept),
        "fp_attributed": len(attributed),
        "hunk_uncovered": uncovered,
        "fp_dropped_unchanged": len(result.dropped_fps),
        "fp_narrowed_dimensions": frontend_marked,
        "locatable": locatable,
        "hunk_files": len(hunks),
        "changed_files": len(changed_files),
    }
    return result
