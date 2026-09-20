"""引擎 · 功能点语义合并（F5）：把「同一件事」的多条功能点收敛为一条。

为什么需要
----------
两条生成通道（代码静态分析 / 地址运行时发现）与多种来源（真实后端 / mock server /
测试桩）会各自产出**语义相同、来源不同**的功能点。若只按 `fp_id` 判重，它们必然各自
成条——`fp_id` 含 `file_path`，来源不同则编号必然不同：

    功能点 × N  →  测试点 × N  →  用例 × N        （重复计数，覆盖率与通过率同步失真）

最典型的真实案例：被测仓库里 `frontend/mock-server/server.py` 用同名路由模拟后端，
与真实后端路由**逐条重复**（旧平台记为 C-②3）。

判定口径（v1.1 · 保守优先：宁可重复，不可误删）
----------------------------------------------
1. **层类型对齐**：只在同一「来源族」内比较名称——
   - `api`：`METHOD + 路径`（`GET /x` 与 `get /x/` 视为同一条）；
   - `page` / `ui`：**同族**（都是前端页面级产物，执行层同为 UI）；
   - `component`（元素粒度）与 `business`（业务函数）**不参与**语义合并——
     它们的 `name` 是文件名 / 限定函数名（如两个目录下各有一个 `SearchBar.tsx`），
     跨目录同名会撞车，按语义合并会**误删真实功能点**。
2. **名称归一**：路径类名称做形态归一——去 query / fragment、折叠重复斜杠、去尾斜杠、
   统一路径参数占位符（`{id}` / `<int:id>` / `:id` → `{}`）。**不折大小写**：
   `/A` 与 `/a` 在 URL 里可以是两个资源，强行折小写属过度合并。
3. **来源优先级**：冲突时保留 `(主优先级, 声明质量)` 更大的那条——
   - 主优先级：运行时（`runtime:`，线上真实可达）3 > 真实源码 2 > 低可信桩
     （路径含 mock / stub / faker / demo / fixture）0；
   - 声明质量（仅页面族）：页面**自己的文件**优先于「菜单 / 抽屉 / 组件里顺带声明的
     同一路径」（真实仓库实测，见 `source_rank`）；
   - 同分保留**先出现**者，保证跨运行、跨机器结果稳定。

不静默
------
每次合并都返回统计与被收敛项的示例，由调用方写入运行备注——被合并掉的功能点**不会**
凭空消失而不留痕迹（对照 F10a「配了以为生效」的静默陷阱）。
"""

from __future__ import annotations

import re
from typing import Any

from core.contracts import FunctionalPoint
from core.enums import FType


# 判定口径版本：口径一旦变化即升版，便于回溯「历史数字是按哪版口径算出来的」。
# v1.1：来源优先级细化为 (主优先级, 声明质量) —— 页面自己的文件优先于「菜单/组件里
#       顺带声明的同一路径」（真实仓库实测：抽屉菜单的 MENUS 表与页面文件都声明了 /m/schedule）。
MERGE_RULE_VERSION = "1.1"

# ---- 来源族（只有同族才比较名称）----
_FAMILY_PAGE = "page"
_FAMILY_API = "api"
_MERGE_FAMILIES: dict[str, str] = {
    FType.PAGE.value: _FAMILY_PAGE,
    FType.UI.value: _FAMILY_PAGE,
    FType.API.value: _FAMILY_API,
}

# ---- 来源可信度 ----
_RUNTIME_PREFIX = "runtime:"
_RANK_RUNTIME = 3
_RANK_SOURCE = 2
_RANK_LOW_TRUST = 0
# 低可信来源标记（路径片段命中即降级）：测试桩 / mock / 演示数据。
_LOW_TRUST_HINTS: tuple[str, ...] = ("mock", "stub", "faker", "demo", "fixture")

# 页面「拥有者」判定：路由声明在页面自己的文件里，比声明在菜单/抽屉/组件里更权威。
_PAGE_DECL_DIRS: frozenset[str] = frozenset({"pages", "views", "screens"})
_PAGE_FILE_RE = re.compile(r"(page|screen)\.[a-z0-9]+$")

# ---- 名称归一 ----
# 路径参数占位符：先统一形态，再折叠斜杠，顺序不可颠倒。
_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\{[^}]*\}"),  # FastAPI / OpenAPI：/invoices/{invoice_id}
    re.compile(r"<[^>]*>"),  # Flask：/invoices/<int:invoice_id>
    re.compile(r":[A-Za-z_][A-Za-z0-9_]*"),  # 前端路由 / Express：/invoices/:id
)
_SLASHES_RE = re.compile(r"/+")
_API_NAME_RE = re.compile(r"^(?P<method>[A-Za-z]+)\s+(?P<path>\S.*)$")

_MAX_EXAMPLES = 5
_EXAMPLE_SEP = "；示例："


# ============================================================================
# 归一与判序
# ============================================================================
def normalize_path(path: str) -> str:
    """路径形态归一：去 query/fragment → 统一占位符 → 折叠斜杠 → 去尾斜杠。

    只做**形态**归一，不做语义猜测：不改大小写、不改动路径分段，
    因此不会把两个真实不同的路由并成一个。
    """
    raw = (path or "").strip()
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    for pattern in _PLACEHOLDER_PATTERNS:
        raw = pattern.sub("{}", raw)
    raw = _SLASHES_RE.sub("/", raw)
    if len(raw) > 1 and raw.endswith("/"):
        raw = raw[:-1]
    return raw


def _api_key(name: str) -> str | None:
    """接口的语义键：`METHOD + 归一化后的路径`；不像「方法 + 路径」则返回 None。"""
    match = _API_NAME_RE.match(name)
    if not match:
        return None
    path = normalize_path(match.group("path"))
    if not path:
        return None
    return f"{_FAMILY_API}|{match.group('method').upper()} {path}"


def semantic_key(fp: FunctionalPoint) -> str | None:
    """功能点的语义键；返回 None 表示**不参与**语义合并（component / business / 非路径名）。

    返回 None 的项在合并时**原样保留**——这是「宁可重复，不可误删」的落点。
    """
    family = _MERGE_FAMILIES.get(str(getattr(fp, "ftype", "")))
    name = str(getattr(fp, "name", "") or "").strip()
    if family is None or not name:
        return None
    if family == _FAMILY_API:
        return _api_key(name)
    if str(getattr(fp, "ftype", "")) == FType.UI.value:
        # ui 元素级功能点：名称形态为 `路径#类型:文字|选择器`，`#` 是**合成分隔符**
        # 而非 URL fragment。若经 `normalize_path` 会按 `#` 截断，使整页元素功能点
        # 全部塌成 `page|/路径` 与同页 `page` 功能点撞键被误并（#221 元素级分解在
        # 完整流水线里被 F5 撤销）。故用完整名作判别键，保证「页面可达」与
        # 「元素可交互」两类功能点各自保留、互不相吞。
        if not name.startswith("/"):
            return None
        return f"{_FAMILY_PAGE}|ui|{name}"
    if not name.startswith("/"):
        return None  # 不像路由（如组件名 `SearchBar`）→ 不参与合并
    return f"{family}|{normalize_path(name)}"


def _is_page_owner(path: str) -> bool:
    """该文件是否「自己拥有」这个页面（`pages/ views/ screens/` 目录或 `*Page.*` 文件名）。"""
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return False
    if any(seg.lower() in _PAGE_DECL_DIRS for seg in segments[:-1]):
        return True
    return bool(_PAGE_FILE_RE.search(segments[-1].lower()))


def source_rank(file_path: str, ftype: str = "") -> tuple[int, int]:
    """来源可信度，返回**可比较的二元组** `(主优先级, 声明质量)`。

    主优先级：运行时（`runtime:`，线上真实可达）3 > 真实源码 2 > 低可信桩 0——主优先级
    永远压过声明质量，保证「运行时优先于静态」这条铁律不被打破。

    声明质量（仅页面族取 1）：页面**自己的文件**（`pages/ views/ screens/` 或 `*Page.*`）
    优先于「菜单 / 抽屉 / 组件里顺带声明的同一路径」。真实仓库实测：`/m/schedule` 既出现在
    `pages/mobile/MobileMePage.tsx`，也出现在 `components/mobile/HistoryDrawer.tsx` 的
    菜单表（只有 path + label）里；若让抽屉文件胜出，该页面功能点的 `module` 与语义描述
    会挂在「菜单」上，而不是那个页面。
    """
    norm = (file_path or "").replace("\\", "/").strip()
    low = norm.lower()
    if low.startswith(_RUNTIME_PREFIX):
        return (_RANK_RUNTIME, 0)
    segments = [seg for seg in low.split("/") if seg]
    low_trust = any(hint in seg for seg in segments for hint in _LOW_TRUST_HINTS)
    main = _RANK_LOW_TRUST if low_trust else _RANK_SOURCE
    owned = ftype in (FType.PAGE.value, FType.UI.value) and _is_page_owner(norm)
    return (main, 1 if owned else 0)


# ============================================================================
# 合并
# ============================================================================
def merge_functional_points(
    base: list[FunctionalPoint],
    incoming: list[FunctionalPoint],
    *,
    element_count: int | None = None,
) -> tuple[list[FunctionalPoint], dict[str, Any]]:
    """把 `incoming` 并入 `base`，按语义键收敛重复项。

    返回 `(合并后的集合, 统计)`；统计键：
      - `added`：incoming 中语义键首次出现的条数；
      - `replaced`：incoming 命中已有键且**优先级更高**、覆盖了原保留项的条数；
      - `deduped`：被收敛掉（未保留）的条数；
      - `examples`：被收敛项的示例（最多 `_MAX_EXAMPLES` 条），用于「不静默」上报。

    顺序稳定性：`base` 原有顺序不变；`incoming` 仅追加。同优先级时保留**先出现**者。

    `element_count`（可选，G-13）：传入运行时元素数后，合并结束前调用量级守护
    `magnitude_guard`。异常时**只 warning、不阻断**合并结果（对照 F10a 静默陷阱）。
    """
    stats: dict[str, Any] = {
        "added": 0,
        "replaced": 0,
        "deduped": 0,
        "examples": [],
        "conflicts": [],
    }
    merged: list[FunctionalPoint] = []
    index: dict[str, int] = {}
    for group, is_incoming in ((base, False), (incoming, True)):
        for fp in group:
            _place(merged, index, fp, stats, is_incoming=is_incoming)
    if element_count is not None:
        _check_magnitude(merged, element_count, stats)
    return merged, stats


def _check_magnitude(
    merged: list[FunctionalPoint], element_count: int, stats: dict[str, Any]
) -> None:
    """G-13 接线：校验量级并在异常时告警（不阻断）。结论回写 stats 供报告呈现。"""
    from engine.magnitude_guard import warn_if_anomalous

    verdict = warn_if_anomalous(len(merged), element_count)
    stats["magnitude"] = verdict.to_dict()


def _source_of(file_path: str) -> str:
    """功能点来自哪条通道：地址运行时发现的 `file_path` 以 `runtime:` 开头 → `url`，
    其余（真实源码 / 低可信桩）→ `code`。用于合并冲突标注的「维度」维度。"""
    norm = (file_path or "").replace("\\", "/").lower()
    return "url" if norm.startswith(_RUNTIME_PREFIX) else "code"


def _reason(winner: tuple[int, int], loser: tuple[int, int]) -> str:
    """冲突保留原因（人类可读，不含凭证）。按来源主优先级判定。"""
    w_main, l_main = winner[0], loser[0]
    if w_main == 3 and l_main == 2:
        return "运行时(地址通道)优先于静态源码"
    if w_main == 3 and l_main == 0:
        return "运行时(地址通道)优先于低可信桩"
    if w_main == 2 and l_main == 0:
        return "真实源码优先于低可信桩(mock/stub/faker/demo/fixture)"
    if w_main == l_main:
        return "同源同名保留首次出现(平级)"
    return "来源优先级高者保留"


def _place(
    merged: list[FunctionalPoint],
    index: dict[str, int],
    fp: FunctionalPoint,
    stats: dict[str, Any],
    *,
    is_incoming: bool,
) -> None:
    """把单个功能点落到结果集合：无键则直放，有键则按来源优先级决出保留者，
    并**结构化记录冲突**（不再静默）。"""
    key = semantic_key(fp)
    if key is None:
        merged.append(fp)
        return
    pos = index.get(key)
    if pos is None:
        index[key] = len(merged)
        merged.append(fp)
        if is_incoming:
            stats["added"] += 1
        return
    kept = merged[pos]
    r_in = source_rank(fp.file_path, str(fp.ftype))
    r_kept = source_rank(kept.file_path, str(kept.ftype))
    if r_in > r_kept:
        merged[pos] = fp
        if is_incoming:
            stats["replaced"] += 1
        else:
            stats["deduped"] += 1
        _record_conflict(stats, key=key, winner=fp, loser=kept)
    else:
        stats["deduped"] += 1
        _record_conflict(stats, key=key, winner=kept, loser=fp)


def _record_conflict(
    stats: dict[str, Any],
    *,
    key: str | None,
    winner: FunctionalPoint,
    loser: FunctionalPoint,
) -> None:
    """登记一条合并冲突（结构化，供报告标注「冲突的测试用例」）。

    含：语义键、胜出方（来源维度 / 文件 / 语义 / 模块）、落败方同上、保留原因。
    同时保留一条一行的 `examples` 文本（向后兼容 `summary_line` 与运行备注）。
    绝不含任何凭证——`file_path` 对运行时项是 `runtime:<url>`（已脱敏）、对源码项是相对路径。
    """
    w_src = _source_of(winner.file_path)
    l_src = _source_of(loser.file_path)
    reason = _reason(
        source_rank(winner.file_path, str(winner.ftype)),
        source_rank(loser.file_path, str(loser.ftype)),
    )
    rec = {
        "semantic_key": key,
        "ftype": str(winner.ftype),
        "name": str(winner.name),
        "survivor_source": w_src,
        "survivor": {
            "file_path": winner.file_path,
            "semantic": getattr(winner, "semantic", "") or "",
            "module": getattr(winner, "module", "") or "",
        },
        "dropped_source": l_src,
        "dropped": {
            "file_path": loser.file_path,
            "semantic": getattr(loser, "semantic", "") or "",
            "module": getattr(loser, "module", "") or "",
        },
        "reason": reason,
    }
    stats["conflicts"].append(rec)
    examples: list[str] = stats["examples"]
    if len(examples) < _MAX_EXAMPLES:
        examples.append(
            f"{winner.ftype}『{winner.name}』({w_src}) 覆盖 {loser.ftype}『{loser.name}』({l_src})：{reason}"
        )


def dedupe_functional_points(
    fps: list[FunctionalPoint],
) -> tuple[list[FunctionalPoint], dict[str, Any]]:
    """同一批功能点内部去重（代码通道自身的重复：真实后端 vs mock server）。

    等价于 `merge_functional_points([], fps)`，但统计以「收敛掉多少条」为准。
    """
    merged, stats = merge_functional_points([], fps)
    stats["input"] = len(fps)
    stats["kept"] = len(merged)
    stats["deduped"] = len(fps) - len(merged)
    return merged, stats


def summary_line(stats: dict[str, Any] | None, label: str) -> str | None:
    """把合并统计压成一行运行备注；**未发生任何收敛时返回 None**（不制造噪声）。"""
    if not stats:
        return None
    replaced = int(stats.get("replaced", 0))
    deduped = int(stats.get("deduped", 0))
    if not replaced and not deduped:
        return None
    parts: list[str] = []
    if replaced:
        parts.append(f"覆盖同名 {replaced} 条")
    if deduped:
        parts.append(f"收敛重复 {deduped} 条")
    line = f"{label}（口径 v{MERGE_RULE_VERSION}）：{'，'.join(parts)}"
    examples = list(stats.get("examples") or [])
    if examples:
        line += _EXAMPLE_SEP + "；".join(examples)
    return line
