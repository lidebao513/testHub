"""引擎 · T5 生成后自检（quality）：命题门禁 + 三 KPI + 三通道。

**这个模块要回答的问题**：这轮生成「到底覆盖了多少」，以及「有多少条其实是水货」。

T1~T4 把用例数从 2 拉到 15，但「15 条」本身不是质量证明——它可能包含
「没有证据指向改动行」「预期里没有可观测对象」「来源标签丢了」的凑数条目。
所以本模块做三件事，顺序不能反：

1. **门禁**（`apply_gate`）：逐条自检，不合规的置 `unverified=True` 并写
   `unverified_reason`（可审计）。门禁**不删测试点**——删掉会让人以为「本来就没
   这个维度」，标黄 + 记原因才是诚实的做法。
2. **KPI**（`compute_kpis`）：三个覆盖率，分子只算**过门禁**的条目，避免水货抬高覆盖。
3. **三通道**（`channel_summary`）：本体 / 影响面 / 运行时 各自计数，且**三栏之和
   必须等于总数**（单测锁死这一条：既不能重复计数，也不能漏计）。

KPI 口径（与任务文档一致，分母全部来自本次运行的真实输入）

- `changed_func_coverage` = 被覆盖的改动函数 / 全部改动函数。
  「改动函数」由本次 hunk 行号反推：Python 走 AST 取最内层函数，TS/JS 无 AST 用
  `file_impact.enclosing_symbols` 的顶层声明显式近似（认不出来记 `<module>`，不硬编名字）。
  「被覆盖」有两条合法路径：**本体**（该函数自己有功能点，且功能点的测试点过门禁）或
  **归属**（T1 把它归属到某个入口功能点，该入口的测试点过门禁）。第二条必须算——
  私有函数被调用图吞并后再补回入口，本来就是 T1 的正解，不算覆盖会低估。
- `impact_entry_coverage` = 已产出影响用例的入口 / 识别出的受影响入口（后端 impact
  可达入口 + 前端 identified 引用方）。这一项本来就由 T2/T4 的 plan 给出，此处只做合并。
- `dimension_coverage` = 实际维度种类数 / 该变更适用维度矩阵理论值（按维度去重）。
  理论值 = 对每个保留功能点取 `plan_of(fp) ∩ scopes` 再并上语义维度，**不按每功能点
  上限截断**——上限是「预算」不是「应测」；被预算挤掉的维度正是这条 KPI 要暴露的。

**与文档的一处口径修正**：文档把「expect 无可观测对象」写成门禁条件，但没说怎么判。
这里用可观测标记词（返回码/字段/显示/不出现…）判定，判不出就拦下——宁可漏放不可错放，
因为漏放的代价是 KPI 虚高，错放的代价只是多一条待人工复核。
"""

from __future__ import annotations

import re
from typing import Any

from core.contracts import FunctionalPoint, TestPoint
from engine import file_impact, incremental_narrow, tp_expand


# ---------------------------------------------------------------------------
# 门禁原因（写入 TestPoint.unverified_reason，值的集合即契约）
# ---------------------------------------------------------------------------
GATE_NO_EVIDENCE = "no_evidence"  # evidence 为空：断言指不到任何改动行/运行时元素
GATE_NO_OBSERVABLE = "no_observable_expect"  # expect 里没有可观测对象（没法判定成败）
GATE_NO_ORIGIN = "no_origin"  # origin 为空：分不清本体/影响面/运行时，无法归因
# T3 既有：该维度需要特殊环境（并发/双身份/慢依赖），诚实标未验证——**不是**水货，
# 所以它与上面三道门禁分开：不计入「门禁拦下」，但仍计入 KPI（它确实覆盖了一个维度）。
GATE_BEST_EFFORT = "best_effort"

# 门禁类原因（命中即不进 KPI 分子）
GATE_REASONS: tuple[str, ...] = (GATE_NO_EVIDENCE, GATE_NO_OBSERVABLE, GATE_NO_ORIGIN)

# ---------------------------------------------------------------------------
# 三通道
# ---------------------------------------------------------------------------
CHANNEL_BODY = "body"
CHANNEL_IMPACT = "impact"
CHANNEL_RUNTIME = "runtime"
CHANNELS: tuple[str, ...] = (CHANNEL_BODY, CHANNEL_IMPACT, CHANNEL_RUNTIME)

# 地址通道（运行时 UI 发现）的功能点用 `runtime:` 前缀标注 file_path（见 pipeline._RUNTIME_PREFIX）
_RUNTIME_PREFIX = "runtime:"
# 影响面来源前缀：impact（T2）/ impact_file（T4）。用前缀而不是白名单，
# 以后新增影响面来源（如 props 追踪）不会被静默归到「本体」栏。
_IMPACT_ORIGIN_PREFIX = "impact"

# expect 的可观测标记：返回码、字段、可见性、不变量。少而准，靠单测覆盖典型写法。
# 「交互/元素/状态变化/分支/为空/区分」是首跑校准补进来的：实测「交互元素可用，点击后
# 产生预期状态变化」与「两者被区分处理：空值走…分支」这两条本来是合格预期，却被判成
# 「无可观测对象」——门禁的假阳性比漏放更伤信任（会让人开始无视标黄）。
_OBSERVABLE_TOKENS: tuple[str, ...] = (
    "返回",
    "响应",
    "状态码",
    "含",
    "等于",
    "显示",
    "提示",
    "渲染",
    "可见",
    "跳转",
    "文案",
    "字段",
    "列表",
    "条数",
    "交互",
    "元素",
    "状态变化",
    "分支",
    "为空",
    "区分",
    "不出现",
    "不重复",
    "不遗漏",
    "不残留",
    "不报错",
    "不白屏",
    "不新增",
    "无新增",
    "可交互",
    "可点击",
    "hasMore",
    "undefined",
    "KeyError",
)
# 三位 HTTP 状态码（200/401/…) 本身就是可观测对象
# B6：排除「200 条 / 500ms / 300 个」这类纯数量/单位——是数值不是状态码，
# 紧跟量词/时间尺寸单位时不当作可观测对象；状态码本身（200/401…）仍算。
_STATUS_RE = re.compile(
    r"(?<!\d)[1-5]\d{2}(?!\d)(?!\s*(?:条|个|项|次|人|天|元|ms|毫秒|秒|px|像素|%|％))"
)


def observable_expect(expect: str) -> bool:
    """预期结果里是否有**可观测对象**（能判定成败的具体东西）。

    反例：「应当正常工作」「符合预期」——没有对象，执行完也无法说清对错。
    正例：「返回 2xx，响应体包含 id」「列表条数不重复增长」「页面不白屏」。
    """
    text = str(expect or "").strip()
    if len(text) < 4:
        return False
    if _STATUS_RE.search(text):
        return True
    return any(token in text for token in _OBSERVABLE_TOKENS)


def channel_of(tp: TestPoint) -> str:
    """测试点归属哪一栏：本体 / 影响面 / 运行时。

    判定顺序有意为之：先看 origin（影响面来源最明确），再看 source 的 `runtime:`
    前缀（运行时发现的页面 origin 同样是 rule），最后落到本体。
    """
    if str(tp.origin or "").startswith(_IMPACT_ORIGIN_PREFIX):
        return CHANNEL_IMPACT
    if str(tp.source or "").startswith(_RUNTIME_PREFIX):
        return CHANNEL_RUNTIME
    return CHANNEL_BODY


def gate_failed(tp: TestPoint) -> bool:
    """该测试点是否被门禁拦下（= 不计入 KPI 分子）。"""
    return str(tp.unverified_reason or "") in GATE_REASONS


def observable_count(tps: list[TestPoint]) -> int:
    """过门禁的测试点数（KPI 分子的统一口径）。"""
    return sum(1 for tp in tps if not gate_failed(tp))


# ============================================================================
# 1) 门禁
# ============================================================================
def apply_gate(
    tps: list[TestPoint], *, enabled: bool = True, fps: list[FunctionalPoint] | None = None
) -> dict[str, Any]:
    """生成后自检：逐条过三道门禁，命中即置 `unverified` 并记原因。

    幂等：重复调用不会把已经标好的原因改回（除了更严重的门禁原因会覆盖
    `best_effort`——「需要特殊环境」比「压根没证据」轻，让严重的说法胜出）。

    B6 口径（用户确认）：evidence 非空即视为有据（不强制指向当前 hunk——前端用例
    无行号，强校验会把整片打成不可信）；但把 evidence 的**去重统计**写进 stats，
    让「同一证据被反复引用造成 KPI 虚高」可观测。`fps` 仅用于识别证据指向的文件
    是否存在于本次功能点集合（软统计，不影响判定）。
    """
    # B6：evidence 去重统计（可观测，不改门禁判定）。fps 保留在签名里兼容调用方，
    # 本口径不强制「指向当前 hunk」——前端用例无行号，强校验会误伤整片。
    del fps
    evidence_all = [str(e) for tp in tps for e in (tp.evidence or [])]
    evidence_unique = set(evidence_all)
    stats: dict[str, int] = {
        "quality_gated": len(tps),
        "gate_blocked": 0,
        GATE_NO_EVIDENCE: 0,
        GATE_NO_OBSERVABLE: 0,
        GATE_NO_ORIGIN: 0,
        GATE_BEST_EFFORT: 0,
        "evidence_total": len(evidence_all),
        "evidence_unique": len(evidence_unique),
        "evidence_duplicate": len(evidence_all) - len(evidence_unique),
    }
    for tp in tps:
        tp.channel = tp.channel or channel_of(tp)
        # T3 既有的 best_effort 标记补上原因，报告才能区分「跑不了」与「没证据」
        if tp.unverified and not tp.unverified_reason:
            tp.unverified_reason = GATE_BEST_EFFORT
        if not enabled:
            continue
        reason = ""
        if not tp.evidence:
            reason = GATE_NO_EVIDENCE
        elif not observable_expect(tp.expect):
            reason = GATE_NO_OBSERVABLE
        elif not str(tp.origin or "").strip():
            reason = GATE_NO_ORIGIN
        if reason:
            tp.unverified = True
            tp.unverified_reason = reason
            stats[reason] += 1
            stats["gate_blocked"] += 1
    stats[GATE_BEST_EFFORT] = sum(1 for tp in tps if tp.unverified_reason == GATE_BEST_EFFORT)
    return stats


# ============================================================================
# 2) 三通道
# ============================================================================
def channel_summary(tps: list[TestPoint]) -> dict[str, Any]:
    """三通道计数（本体 / 影响面 / 运行时）。

    `consistent` 是三栏之和 == 总数的自检结果——上游单测会断言它为 True，
    因为「分栏重复计数」是最容易悄悄发生、又最难在报告里被发现的错误。

    `gate_ok` 用 `unverified` 标志判定（而不是门禁原因）：这一栏要回答的是
    「这些点能不能直接拿去跑」，best_effort（需特殊环境）同样跑不了，必须排除；
    而 KPI 分子只看门禁原因（best_effort 覆盖的维度是真实存在的，算覆盖）。
    两个口径是**有意不同**的，别合并。
    """
    out: dict[str, Any] = {name: {"tp": 0, "gate_ok": 0} for name in CHANNELS}
    for tp in tps:
        name = tp.channel if tp.channel in out else channel_of(tp)
        out[name]["tp"] += 1
        if not tp.unverified:
            out[name]["gate_ok"] += 1
    total = sum(int(v["tp"]) for v in out.values())
    out["total"] = len(tps)
    out["consistent"] = total == len(tps)
    return out


# ============================================================================
# 3) KPI
# ============================================================================
def _file_of(tp: TestPoint) -> str:
    return str(tp.source or "").replace("\\", "/")


def changed_functions(
    files: dict[str, Any], hunks: dict[str, list[tuple[int, int]]]
) -> list[tuple[str, str]]:
    """本次 hunk 落在哪些「函数/顶层声明」里（跨所有改动文件，去重保序）。

    Python 用 AST 取最内层函数名（与影响面/归属同一口径，绝不另起一套解析）；
    TS/JS/Vue 没有 AST，用「行首无缩进的声明正则」近似顶层边界。认不出来时记
    `<module>`——模块级改动也是一个需要覆盖的单位，不能直接丢弃（丢分母会让
    覆盖率虚高）。
    """
    out: list[tuple[str, str]] = []
    for rel, ranges in (hunks or {}).items():
        sf = files.get(rel) or files.get(rel.replace("\\", "/"))
        if sf is None:
            continue
        names: list[str] = []
        graph = incremental_narrow.file_call_graph(sf) if getattr(sf, "is_python", False) else None
        if graph is not None:
            for start, _length in ranges or []:
                names.append(incremental_narrow._innermost_func(graph, int(start)) or "<module>")
        else:
            names.extend(
                file_impact.enclosing_symbols(getattr(sf, "text", ""), ranges) or ["<module>"]
            )
        for name in dict.fromkeys(names):
            out.append((str(rel).replace("\\", "/"), name))
    return out


def _fp_symbols(fps: list[FunctionalPoint], files: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """功能点 → (文件, 所在函数名)。取不到的（前端无行号）不进这张表。"""
    graphs: dict[str, Any] = {}
    out: dict[str, tuple[str, str]] = {}
    for fp in fps:
        parsed = incremental_narrow._parse_line(fp)
        if not parsed:
            continue
        rel, line = parsed
        if rel not in graphs:
            sf = (files or {}).get(rel) or (files or {}).get(rel.replace("\\", "/"))
            graphs[rel] = incremental_narrow.file_call_graph(sf) if sf is not None else None
        graph = graphs[rel]
        if graph is None:
            continue
        inner = incremental_narrow._innermost_func(graph, line)
        if inner:
            out[fp.fp_id] = (rel, inner)
    return out


def _covered_fp_ids(tps: list[TestPoint]) -> set[str]:
    """有 ≥1 条过门禁测试点的功能点（KPI 的「覆盖」原子事实）。"""
    return {tp.fp_contract_id for tp in tps if tp.fp_contract_id and not gate_failed(tp)}


def changed_func_coverage(
    tps: list[TestPoint],
    *,
    fps: list[FunctionalPoint],
    files: dict[str, Any],
    hunks: dict[str, list[tuple[int, int]]],
    attributed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """改动函数覆盖率：被覆盖的改动函数 / 全部改动函数。"""
    changed = changed_functions(files, hunks)
    covered_fps = _covered_fp_ids(tps)
    fp_symbols = _fp_symbols(fps, files)
    # 归属补救：T1 的「途经函数 → 入口功能点」，让私有函数的改动也能算被覆盖
    by_attr: dict[tuple[str, str], set[str]] = {}
    for ev in attributed or []:
        rel = str(ev.get("file_path") or "").replace("\\", "/")
        via = str(ev.get("via") or "")
        fp_id = str(ev.get("fp_id") or "")
        if rel and via and fp_id:
            by_attr.setdefault((rel, via), set()).add(fp_id)
    # 前端无行号：按「文件被覆盖」判定（符号级需 TS 语义分析，二期）。
    # 只收**没有行号**的功能点：有行号的后端功能点若也进这张表，某个函数明明没被测点
    # 覆盖，也会被同文件的另一个功能点「顺带算覆盖」——覆盖率立刻虚高。
    by_file: dict[str, set[str]] = {}
    for fp in fps:
        rel = str(fp.file_path or "").replace("\\", "/")
        if rel and fp.fp_id not in fp_symbols:
            by_file.setdefault(rel, set()).add(fp.fp_id)

    uncovered: list[dict[str, str]] = []
    covered = 0
    for rel, symbol in changed:
        direct = {fp_id for fp_id, key in fp_symbols.items() if key == (rel, symbol)}
        candidates = direct | by_attr.get((rel, symbol), set())
        if not candidates:
            # 认不出符号级归属时退回文件级：前端没有 AST、或 hunk 落在模块级。
            # Python 文件这条兜底天然是空集（有行号的功能点都进了 fp_symbols），
            # 所以不会把「后端某函数没覆盖」误算成覆盖。
            candidates = by_file.get(rel, set())
        if candidates & covered_fps:
            covered += 1
        else:
            uncovered.append({"file": rel, "symbol": symbol})
    total = len(changed)
    return {
        "value": round(covered / total, 3) if total else 1.0,
        "covered": covered,
        "total": total,
        "uncovered": uncovered[:20],
    }


def impact_entry_coverage(
    *,
    impact_stats: dict[str, Any] | None = None,
    file_impact_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """影响入口覆盖率：已产出影响用例的入口 / 识别出的受影响入口。

    后端（T2）与前端（T4）各有一套「入口」概念，这里只做合并求和——
    分别保留 `backend` / `frontend` 明细，便于报告定位是哪一侧没覆盖到。
    """
    backend = dict(impact_stats or {})
    frontend = dict(file_impact_stats or {})
    b_total = int(backend.get("impact_reachable_entries", 0) or 0)
    b_covered = int(backend.get("impact_items", 0) or 0)
    f_total = int(frontend.get("file_impact_identified", 0) or 0)
    f_covered = int(frontend.get("file_impact_items", 0) or 0)
    total = b_total + f_total
    covered = b_covered + f_covered
    return {
        "value": round(covered / total, 3) if total else 1.0,
        "covered": covered,
        "total": total,
        "backend": {"covered": b_covered, "total": b_total},
        "frontend": {"covered": f_covered, "total": f_total},
    }


def _theory_dimensions(
    fps: list[FunctionalPoint],
    scopes: set[str] | None,
    semantic: dict[str, list[tuple[str, str]]] | None,
) -> list[str]:
    """该变更适用的维度矩阵理论值（按维度名去重，未按每功能点上限截断）。"""
    scope_set = set(scopes or ())
    dims: list[str] = []
    for fp in fps:
        pairs = list(tp_expand.plan_of(fp))
        pairs += list((semantic or {}).get(fp.fp_id) or ())
        for category, dimension in pairs:
            if scope_set and category not in scope_set:
                continue
            if dimension and dimension not in dims:
                dims.append(dimension)
    return dims


def dimension_coverage(
    tps: list[TestPoint],
    *,
    fps: list[FunctionalPoint],
    scopes: set[str] | None = None,
    semantic: dict[str, list[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    """维度覆盖率：实际维度种类 / 适用维度矩阵理论值（都按维度去重）。"""
    theory = _theory_dimensions(fps, scopes, semantic)
    actual = {tp.dimension for tp in tps if tp.dimension and not gate_failed(tp)}
    missing = [d for d in theory if d not in actual]
    return {
        "value": round(len(actual) / len(theory), 3) if theory else 1.0,
        "actual": len(actual),
        "theory": len(theory),
        "missing": missing[:20],
    }


def compute_kpis(  # noqa: PLR0913 - 三个 KPI 的输入各异，逐个显式关键字比包一层「上下文」更清楚
    tps: list[TestPoint],
    *,
    fps: list[FunctionalPoint],
    files: dict[str, Any] | None = None,
    hunks: dict[str, list[tuple[int, int]]] | None = None,
    scopes: set[str] | None = None,
    semantic: dict[str, list[tuple[str, str]]] | None = None,
    attributed: list[dict[str, Any]] | None = None,
    impact_stats: dict[str, Any] | None = None,
    file_impact_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """三个 KPI 汇总（统一入口，报告与前端都只读这一份结构）。"""
    changed = changed_func_coverage(
        tps, fps=fps, files=files or {}, hunks=hunks or {}, attributed=attributed
    )
    impact = impact_entry_coverage(impact_stats=impact_stats, file_impact_stats=file_impact_stats)
    dims = dimension_coverage(tps, fps=fps, scopes=scopes, semantic=semantic)
    return {
        "changed_func_coverage": changed,
        "impact_entry_coverage": impact,
        "dimension_coverage": dims,
        # 便于报告一句话说明「分母是什么」：三个 KPI 的分母口径差异很大，不写清会被误读
        "note": (
            "分子只统计过门禁的测试点；changed_func 的分母是本次 hunk 落到的函数"
            "（前端无 AST 时按顶层声明近似），dimension 的分母是按维度去重后的适用矩阵"
            "（未按每功能点上限截断，故 <100% 属预算收敛的正常结果）。"
        ),
    }


def summarize(tps: list[TestPoint]) -> dict[str, Any]:
    """报告用的一句话摘要（避免调用方各自拼口径）。"""
    total = len(tps)
    ok = observable_count(tps)
    return {
        "total": total,
        "gate_ok": ok,
        "blocked": total - ok,
        "gate_pass_rate": round(ok / total, 3) if total else 1.0,
    }


__all__ = [
    "CHANNELS",
    "CHANNEL_BODY",
    "CHANNEL_IMPACT",
    "CHANNEL_RUNTIME",
    "GATE_BEST_EFFORT",
    "GATE_NO_EVIDENCE",
    "GATE_NO_OBSERVABLE",
    "GATE_NO_ORIGIN",
    "GATE_REASONS",
    "apply_gate",
    "changed_func_coverage",
    "changed_functions",
    "channel_of",
    "channel_summary",
    "compute_kpis",
    "dimension_coverage",
    "gate_failed",
    "impact_entry_coverage",
    "observable_count",
    "observable_expect",
    "summarize",
]
