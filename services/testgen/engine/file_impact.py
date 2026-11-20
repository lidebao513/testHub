"""引擎 · T4 前端文件级跨文件兜底（file_impact）。

**为什么单独一条通道**

后端有 AST 反向调用图（`engine/impact.py`）：谁调了被改函数、几跳可达入口，都能算出来。
前端没有这种东西——`pages/DailyPerfPage.tsx` import 了 `pages/pc/PcChatPage.tsx` 里的
`PcComposer`，两者之间只有一条 import 语句，函数级收窄与调用图都看不见它。于是
「改了被复用的组件，父页面全裸奔」在代码通道里完全不可见。

本模块按 import 边补**文件级**回归用例，三条硬约束：

1. **只对页面/组件做**（`/pages/`、`/components/`、`/views/`）。`api/adapter.ts` 这类薄封装
   被全站 50 个文件 import，给它出「验证某页正常」既测不出东西又会刷屏；那类改动的
   影响面由接口通道（T2）负责。
2. **按引用方聚合**：同一父组件 import 了多个被改文件，只出 1 条（断言列多个来源），
   不按 import 边数量膨胀。
3. 每次最多 `max_total` 条（默认 2）。断言只到「引用区域能渲染、可交互、控制台无新增
   报错」——具体元素的 selector 需要运行时 DOM，凭空编 selector 只会造必定失败的假用例。

**精度分层**（不假装所有边都一样可信）

导入符号（`{ PcComposer }`）能对上被改文件里 hunk 所在的**顶层**声明，判为 `precise`；
对不上时退为「模块级影响」并把置信度降到 0.5——既保留「模块副作用可能影响父页面」的
回归价值，又不冒充符号级影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.contracts import FunctionalPoint, TestPoint, tp_id_of
from core.enums import Dimension, FType, MethodMarker, Tag, TPType, VerifyLayer
from engine import fp_extract
from engine.repo_facts import RepoFacts


# 声明行（向上回溯找 hunk 所属的组件/函数/常量）
_DECL_RE = re.compile(
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
    r"|(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
)
# 单个 hunk 向上回溯的最大行数（组件体通常就在几十行内）
_MAX_SCAN_UP = 300
# 只有这些目录下的前端文件才做「引用方受影响」判定（页面/组件/视图）
_UI_DIR_PARTS = ("/pages/", "/components/", "/views/")


def is_ui_layer(rel: str) -> bool:
    """该前端文件是否属于「页面/组件」层（api/lib/store 等工具层不参与文件级兜底）。"""
    path = "/" + str(rel or "").replace("\\", "/").lstrip("/")
    return any(part in path for part in _UI_DIR_PARTS)


@dataclass
class FileImpactEdge:
    """一条 import 边：谁引用了哪个被改文件、导入了哪些符号。"""

    changed: str
    spec: str = ""
    symbols: list[str] = field(default_factory=list)
    changed_symbols: list[str] = field(default_factory=list)

    @property
    def precise(self) -> bool:
        """导入符号命中被改文件的顶层声明（= 本次真的改了这个符号）。"""
        return bool(set(self.symbols) & set(self.changed_symbols))


@dataclass
class FileImpactItem:
    """一条文件级跨文件影响：某个前端页面/组件引用了本次改动的文件（已按引用方聚合）。"""

    importer: str  # 引用方（父页面/组件）
    edges: list[FileImpactEdge] = field(default_factory=list)
    route: str = ""  # 引用方路由（有则用作地址；空=未知地址，只做区域级断言）
    importers_fp_id: str = ""  # 引用方功能点（若被扫描到）
    changed_fp_id: str = ""  # 兜底锚点（被改文件的功能点）

    @property
    def targets(self) -> list[str]:
        return list(dict.fromkeys(e.changed for e in self.edges))

    @property
    def precise(self) -> bool:
        return any(e.precise for e in self.edges)

    @property
    def primary(self) -> FileImpactEdge | None:
        """主因：符号级命中优先，其次页面/组件（比工具模块更贴近用户可见面）。"""
        if not self.edges:
            return None
        return max(self.edges, key=lambda e: (e.precise, is_ui_layer(e.changed), e.changed))

    @property
    def anchor_fp_id(self) -> str:
        """追溯锚点：优先引用方自身的功能点，否则挂到被改文件的功能点。"""
        return self.importers_fp_id or self.changed_fp_id

    @property
    def area(self) -> str:
        return self.route or self.importer

    @property
    def method(self) -> str:
        return MethodMarker.PAGE.value if self.route else MethodMarker.UI.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "importer": self.importer,
            "route": self.route,
            "targets": self.targets,
            "precise": self.precise,
            "edges": [
                {
                    "changed": e.changed,
                    "spec": e.spec,
                    "symbols": e.symbols,
                    "changed_symbols": e.changed_symbols,
                    "precise": e.precise,
                }
                for e in self.edges
            ],
            "anchor_fp_id": self.anchor_fp_id,
        }


def enclosing_symbols(text: str, ranges: list[tuple[int, int]]) -> list[str]:
    """hunk 行属于哪个**顶层**声明（组件/函数/常量）。

    必须是顶层（行首无缩进）而不是「最近的声明」：改动行常常是组件体内的
    `const onSessionCreated = ...`，最近的声明会返回这个局部变量，而跨文件引用方
    import 的是**组件本身**（`{ PcComposer }`）——拿局部变量去比永远对不上，
    精度分层就退化成「全是文件级」。TS 没有可用 AST，这里用「行首无缩进 + 声明正则」
    近似顶层边界；回溯不到就返回空列表（降级为文件级断言，不硬编名字）。
    """
    lines = str(text or "").splitlines()
    out: list[str] = []
    for start, length in ranges or []:
        begin = max(int(start) - 1, 0)
        end = min(len(lines), begin + max(int(length), 1))
        found = ""
        # 1) 改动行本身是顶层声明（新增的组件/函数/常量就在这一行）
        for line in lines[begin:end]:
            m = _DECL_RE.match(line)
            if m:
                found = m.group(1) or m.group(2) or ""
                break
        # 2) 否则向上回溯最近的顶层声明（函数体缩进的行会被跳过）
        if not found:
            for line in reversed(lines[max(0, begin - _MAX_SCAN_UP) : begin]):
                m = _DECL_RE.match(line)
                if m:
                    found = m.group(1) or m.group(2) or ""
                    break
        if found and found not in out:
            out.append(found)
    return out


def _fps_by_rel(fps: list[FunctionalPoint]) -> dict[str, list[FunctionalPoint]]:
    out: dict[str, list[FunctionalPoint]] = {}
    for fp in fps:
        rel = str(fp.file_path or "").replace("\\", "/")
        if rel:
            out.setdefault(rel, []).append(fp)
    return out


def _pick_fp(candidates: list[FunctionalPoint] | None, prefer: tuple[str, ...]) -> str:
    """从同文件功能点里挑最合适的一条做锚点（页面/组件优先）。"""
    for ftype in prefer:
        for fp in candidates or []:
            if fp.ftype == ftype:
                return fp.fp_id
    return candidates[0].fp_id if candidates else ""


def build_file_impact(  # noqa: PLR0913 - 入参都是显式策略/出参，包成对象反而更难读
    facts: RepoFacts,
    *,
    hunks: dict[str, list[tuple[int, int]]],
    files: dict[str, Any],
    fps: list[FunctionalPoint],
    max_total: int = 2,
    stats: dict[str, int] | None = None,
) -> list[FileImpactItem]:
    """构造文件级影响项：按引用方聚合，只留页面/组件层的 import 边。

    `stats` 是**可选出参**（沿用 `impact.build_impact_plan` 的 plan.stats 思路）：
    回填 `identified`（识别出的引用方总数，预算截断前）与 `truncated`。
    T5 的 impact_entry_coverage 需要「识别出多少」作分母——只返回截断后的列表
    会让覆盖率永远 100%，那是个假指标。
    """
    if not facts or not hunks or max_total <= 0:
        return []
    by_rel = _fps_by_rel(fps)
    grouped: dict[str, FileImpactItem] = {}
    for rel in sorted(hunks):
        norm = str(rel).replace("\\", "/")
        if not is_ui_layer(norm):
            continue  # api/lib/store 等工具层：影响面归接口通道，这里不刷「验证某页正常」
        sf = files.get(rel) or files.get(norm)
        if sf is None:
            continue
        changed_symbols = enclosing_symbols(getattr(sf, "text", ""), hunks[rel])
        changed_fp = _pick_fp(by_rel.get(norm), (FType.PAGE.value, FType.COMPONENT.value))
        for spec in facts.importers_of(norm):
            if not is_ui_layer(spec.importer):
                continue  # 工具层引用方（hooks/lib）没有可执行的 UI 面
            item = grouped.get(spec.importer)
            if item is None:
                route_binding = facts.route_of_component(Path(spec.importer).stem)
                item = FileImpactItem(
                    importer=spec.importer,
                    route=route_binding.path if route_binding else "",
                    importers_fp_id=_pick_fp(
                        by_rel.get(spec.importer), (FType.PAGE.value, FType.COMPONENT.value)
                    ),
                    changed_fp_id=changed_fp,
                )
                grouped[spec.importer] = item
            item.edges.append(
                FileImpactEdge(
                    changed=norm,
                    spec=spec.spec,
                    symbols=list(spec.symbols),
                    changed_symbols=changed_symbols,
                )
            )
            if not item.changed_fp_id:
                item.changed_fp_id = changed_fp

    # 排序：地址真实可执行（有路由）优先 → 符号级命中优先 → 覆盖的被改文件多优先 → 路径稳定
    items = sorted(
        (i for i in grouped.values() if i.anchor_fp_id),
        key=lambda i: (not i.route, not i.precise, -len(i.targets), i.importer),
    )
    keep = max(1, max_total)
    if stats is not None:
        stats["file_impact_identified"] = len(items)
        stats["file_impact_truncated"] = max(0, len(items) - keep)
    return items[:keep]


def _expect_of(item: FileImpactItem) -> str:
    """文件级断言：三段式（输入 → 观察 → 判据），并如实区分符号级/模块级证据。"""
    names = "、".join(Path(t).stem for t in item.targets[:3]) or "被改模块"
    where = item.area
    if item.precise:
        hit = "、".join(sorted({s for e in item.edges if e.precise for s in e.symbols})[:3])
        cause = f"因 {names} 在本次改动中被修改（{hit} 命中改动行）"
    else:
        cause = f"因 {names} 在本次改动中有变更（导入的符号本身不在改动行内，属模块级影响）"
    return (
        f"{cause}，验证引用方 {where} 的相关区域未回归："
        "页面首屏渲染完成（不白屏）、该区域正常显示且按钮/输入可交互，"
        "控制台无新增报错（不断言具体元素，元素级断言留给运行时通道）"
    )


def file_impact_test_points(
    items: list[FileImpactItem],
    *,
    review_status: str = "approved",
) -> list[TestPoint]:
    """文件级影响项 → 测试点（行为维度「正常」：验的是引用区域未回归）。

    标签固定为「受影响·文件级」：本体用例是「更新/全量」，影响面用例必须能一眼分开，
    否则报告里分不清「这条是这次改的」还是「这条是怕改坏的」。
    """
    out: list[TestPoint] = []
    for item in items:
        anchor = item.anchor_fp_id
        if not anchor or not item.edges:
            continue
        dimension = Dimension.IMPACT_FILE.value
        importer_name = Path(item.importer).stem
        names = "、".join(Path(t).stem for t in item.targets[:3])
        evidence = [f"{item.importer} 通过 {e.spec} 导入 {e.changed}" for e in item.edges]
        symbols = sorted({s for e in item.edges for s in e.changed_symbols})
        if symbols:
            evidence.append(f"被改文件的改动落在声明 {'、'.join(symbols[:3])}")
        if item.route:
            evidence.append(f"引用方路由 {item.route}（来自仓库静态路由表）")
        out.append(
            TestPoint(
                tp_id=tp_id_of(
                    anchor,
                    TPType.NORMAL.value,
                    area=item.area,
                    method=item.method,
                    dimension=dimension,
                ),
                fp_contract_id=anchor,
                category=TPType.NORMAL.value,
                module=fp_extract.module_of(item.importer),
                title=f"[正常] {importer_name} 引用 {names} · {dimension}",
                semantic=(
                    f"影响面回归（文件级）：{item.importer} 引用了本次改动的 {names}，"
                    "其引用区域需复测"
                ),
                source=item.targets[0] if item.targets else item.importer,
                method=item.method,
                area=item.area,
                expect=_expect_of(item),
                dimension=dimension,
                tag=Tag.IMPACT_FILE.value,
                review_status=review_status,
                verify_layer=VerifyLayer.UI.value,
                evidence=evidence,
                confidence=0.7 if item.precise else 0.5,
                unverified=False,
                origin="impact_file",
            )
        )
    return out
