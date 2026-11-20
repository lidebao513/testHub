"""引擎 · 变更靶向（change_targeting）—— 把「改了什么」翻译成「该测哪个运行时目标」。

**为什么需要它**
增量模式（`mode=incremental`）只约束代码通道：只对变更函数展开测试点。可一旦同时
提供被测地址，运行时 UI 通道会把整站扫一遍（典型 39 页 / 28 接口 / 132 元素），产出
几百条与「本次改了什么」无关的用例——用户看到「改 5 个文件出 294 条」会直接怀疑质量。

本模块做的是**靶向映射**（纯计算，不读盘、不调 LLM、不碰 pipeline）：
  1. 前端 api 层文件（`src/api/*.ts`）：变更 hunk 覆盖区域里的 `/api/...` 字面量 → 接口目标；
  2. 前端页面文件（`pages/**/XxxPage.tsx`）：文件名拆词 → 候选路由（`PcChatPage` → `/chat`）；
  3. 壳/布局文件（`*Shell`/`*Layout`/`App`/`router*`）：只贡献路由字面量，**不单独产出页面目标**
     （否则一个壳文件会把全站页面都算成变更相关）；
  4. 后端函数：路由装饰器直接得 `METHOD /path`；私有辅助函数反查**一层**调用方再取路由；
  5. 兜底：接口字面量一条都没提取到时，才用符号关键词（`thread`/`session`）做段级模糊匹配。

**保守原则**：宁可不生成，也不刷全站。三条链路都没命中时 `is_empty=True`，由调用方
跳过 URL 用例并如实告知「本次变更未命中可测运行时目标」。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.log import get_logger


log = get_logger(__name__)

# 命中这些片段视为「壳/布局/路由表」：只贡献路由字面量，不产出页面目标。
_SHELL_HINTS = ("shell", "layout", "router", "routes", "menu", "nav", "sidebar")

# 符号拆词后的停用词：这类词在路径里到处都是，留着会把靶向放大成全站。
_STOP_WORDS = frozenset(
    {
        "page",
        "pages",
        "view",
        "views",
        "component",
        "components",
        "helper",
        "helpers",
        "util",
        "utils",
        "index",
        "export",
        "default",
        "const",
        "function",
        "async",
        "await",
        "return",
        "import",
        "props",
        "handler",
        "handle",
        "callback",
        "listener",
        "init",
        "setup",
        "main",
        "base",
        "common",
        "data",
        "item",
        "items",
        "value",
        "values",
        "type",
        "types",
        "name",
        "names",
        "text",
        "with",
        "retry",
        "json",
        "blob",
        "fetch",
        "request",
        "response",
        "result",
        "results",
        "list",
        "lists",
        "limit",
        "offset",
        "params",
        "count",
    }
)

_API_LITERAL_RE = re.compile(r"""['"`](/api/[^'"`\s?#)]*)""")
_TEMPLATE_RE = re.compile(r"\$\{[^}]*\}")
_NUM_SEG_RE = re.compile(r"^\d{4,}$")
_HEX_SEG_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
_UUID_SEG_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ROUTE_DECORATOR_RE = re.compile(
    r"""(?:\w+\.)*(?:get|post|put|patch|delete|route|websocket)\(\s*['"]([^'"]+)['"]""",
    re.I,
)
_ROUTE_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "route", "api_route", "websocket"}
)
_FRONT_ROUTE_RE = re.compile(r"""['"](/[a-zA-Z][\w\-/]{1,60})['"]""")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".vue")


# ============================================================================
# 归一化与小工具
# ============================================================================
def normalize_rel(path: str) -> str:
    """相对路径统一为「正斜杠、无前导 ./」——与 scan/diff 的键口径一致。"""
    p = str(path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def normalize_api_path(path: str) -> str:
    """折叠动态 ID 段与模板变量，得到可用于比较的接口路径。"""
    p = (path or "").split("?")[0].split("#")[0].strip()
    p = _TEMPLATE_RE.sub("{id}", p)
    segs = [
        "{id}" if (_NUM_SEG_RE.match(s) or _UUID_SEG_RE.match(s) or _HEX_SEG_RE.match(s)) else s
        for s in p.split("/")
    ]
    out = "/".join(segs)
    return out if out.startswith("/") else "/" + out


def _normalize_page_path(path: str) -> str:
    p = (path or "").split("?")[0].split("#")[0].strip()
    if "://" in p:
        p = "/" + p.split("://", 1)[1].split("/", 1)[-1] if "/" in p.split("://", 1)[1] else "/"
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def _camel_words(name: str) -> list[str]:
    """`listThreadsPage` → ["list","threads","page"]；`_record_thread_activity` 同理。"""
    words: list[str] = []
    for part in re.split(r"[_\-\s]+", name or ""):
        words.extend(_CAMEL_RE.findall(part))
    return [w.lower() for w in words]


def _keywords(symbols: Iterable[str]) -> list[str]:
    out: list[str] = []
    for sym in symbols:
        for word in _camel_words(sym):
            if len(word) >= 4 and word not in _STOP_WORDS and word not in out:
                out.append(word)
    return out


def _text_of(src: Any) -> str:
    """兼容两种入参：源码字符串，或带 `.text` 的 SourceFile。"""
    if src is None:
        return ""
    if isinstance(src, str):
        return src
    return str(getattr(src, "text", "") or "")


def _is_test_file(rel: str) -> bool:
    """测试文件不参与靶向：它们会 import 全站接口，拿它当依据等于又回到全站巡检。"""
    lower = rel.lower()
    name = lower.rsplit("/", 1)[-1]
    return (
        "/tests/" in lower
        or "/test/" in lower
        or "/__tests__/" in lower
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _is_shell_file(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1].lower()
    stem = base.rsplit(".", 1)[0]
    return any(hint in stem for hint in _SHELL_HINTS)


def _is_page_file(rel: str) -> bool:
    lower = rel.lower()
    if not lower.endswith(_TS_EXT):
        return False
    base = lower.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return (
        "/pages/" in lower or "/views/" in lower or base.endswith("page") or base.endswith("view")
    )


def _is_api_layer_file(rel: str) -> bool:
    lower = rel.lower()
    if not lower.endswith(_TS_EXT):
        return False
    return "/api/" in lower or lower.startswith("api/") or "/services/" in lower


def _hunk_ranges(hunks: Mapping[str, Sequence[tuple[int, int]]], rel: str) -> list[tuple[int, int]]:
    """取该文件的 hunk 行区间；`hunks` 可能是相对路径或带前缀的键，做一次宽松匹配。"""
    if rel in hunks:
        return list(hunks[rel])
    tail = rel.rsplit("/", 1)[-1]
    for key, ranges in hunks.items():
        if normalize_rel(key).endswith("/" + tail) or normalize_rel(key) == rel:
            return list(ranges)
    return []


def _slice_by_ranges(text: str, ranges: Sequence[tuple[int, int]], span: int = 40) -> str:
    """按 hunk 行区间（±span 行）切片；无区间时返回全文。"""
    lines = text.splitlines()
    if not ranges:
        return text
    picked: list[str] = []
    total = len(lines)
    for start, length in ranges:
        lo = max(1, int(start) - span)
        hi = min(total, int(start) + max(int(length), 1) + span)
        picked.extend(lines[lo - 1 : hi])
    return "\n".join(picked)


# ============================================================================
# 靶向计划
# ============================================================================
@dataclass
class TargetPlan:
    """一份「变更 → 运行时目标」的映射结果。"""

    api_paths: list[str] = field(default_factory=list)
    page_paths: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    unmatched_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.api_paths or self.page_paths)

    def match_api(self, path: str) -> bool:
        """运行时接口是否落在靶向内（含子路径，`/threads` 覆盖 `/threads/{id}`）。"""
        p = normalize_api_path(path)
        return any(_path_covers(t, p) for t in self.api_paths)

    def match_page(self, path: str) -> bool:
        p = _normalize_page_path(path)
        return any(_page_covers(t, p) for t in self.page_paths)

    def match_text(self, text: str) -> bool:
        low = (text or "").lower()
        return any(k in low for k in self.keywords)

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_paths": list(self.api_paths),
            "page_paths": list(self.page_paths),
            "keywords": list(self.keywords),
            "reasons": dict(self.reasons),
            "symbols": list(self.symbols),
            "unmatched_files": list(self.unmatched_files),
            "notes": list(self.notes),
        }


def _path_covers(target: str, path: str) -> bool:
    """`target` 是否覆盖 `path`：相等，或 target 是 path 的祖先段。"""
    t = normalize_api_path(target)
    p = normalize_api_path(path)
    return p == t or p.startswith(t.rstrip("/") + "/") or t.startswith(p.rstrip("/") + "/")


def _page_covers(target: str, path: str) -> bool:
    t = _normalize_page_path(target)
    p = _normalize_page_path(path)
    if t == "/":
        return p == "/"
    return p == t or p.startswith(t + "/")


# ============================================================================
# 变更文件解析
# ============================================================================
@dataclass
class _ChangedFacts:
    """从变更文件里抽出来的原始事实（尚未与运行时清单比对）。"""

    api_literals: list[str] = field(default_factory=list)
    page_candidates: list[str] = field(default_factory=list)
    route_literals: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    no_hunk: list[str] = field(default_factory=list)


def _add_unique(bucket: list[str], values: Iterable[str]) -> None:
    for v in values:
        if v and v not in bucket:
            bucket.append(v)


def _api_literals_in(text: str, ranges: Sequence[tuple[int, int]]) -> list[str]:
    found: list[str] = []
    for chunk in _slice_by_ranges(text, ranges).splitlines():
        for m in _API_LITERAL_RE.finditer(chunk):
            p = normalize_api_path(m.group(1))
            if p not in found:
                found.append(p)
    return found


def _page_candidates_from_name(rel: str) -> list[str]:
    """`pages/pc/PcChatPage.tsx` → ["/pc/chat", "/chat"]；壳文件返回空。"""
    if _is_shell_file(rel):
        return []
    stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    words = [w for w in _camel_words(stem) if w not in _STOP_WORDS]
    if not words:
        return []
    cands = ["/" + "/".join(words)]
    if len(words) > 1:
        cands.append("/" + words[-1])
    return cands


def _route_literals_in(text: str) -> list[str]:
    out: list[str] = []
    for m in _FRONT_ROUTE_RE.finditer(text):
        p = _normalize_page_path(m.group(1))
        if p not in out:
            out.append(p)
    return out


def _fn_hit(fn: ast.FunctionDef | ast.AsyncFunctionDef, ranges: Sequence[tuple[int, int]]) -> bool:
    start = int(getattr(fn, "lineno", 0) or 0)
    end = int(getattr(fn, "end_lineno", 0) or start)
    for h_start, h_len in ranges:
        h_end = int(h_start) + max(int(h_len), 1) - 1
        if start <= h_end and int(h_start) <= end:
            return True
    return False


def _route_of_decorators(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """装饰器 → 路由路径。优先按 AST 结构判断（`@router.get("/x")` 的 unparse 不含 `@`）。"""
    for deco in fn.decorator_list:
        call = deco if isinstance(deco, ast.Call) else None
        if call is not None:
            func = call.func
            attr = getattr(func, "attr", "") or getattr(func, "id", "")
            if str(attr).lower() in _ROUTE_METHODS:
                arg0 = call.args[0] if call.args else None
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    return normalize_api_path(arg0.value)
        try:
            seg = ast.unparse(deco)
        except Exception:  # noqa: BLE001  # pragma: no cover - 老版本 Python 无 unparse
            continue
        m = _ROUTE_DECORATOR_RE.search(seg)
        if m:
            return normalize_api_path(m.group(1))
    return ""


def _callers_index(tree: ast.AST) -> dict[str, set[str]]:
    """一层调用图：callee 名 → 调用方函数名集合（只看直接调用）。"""
    out: dict[str, set[str]] = {}

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call):
                name = ""
                func = child.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name and owner:
                    out.setdefault(name, set()).add(owner)
            visit(child, owner)

    visit(tree, "")
    return out


def _backend_facts(text: str, ranges: Sequence[tuple[int, int]]) -> tuple[list[str], list[str]]:
    """后端源文件 → (接口路径, 变更符号名)。

    命中 hunk 的函数若带路由装饰器，直接得接口；私有辅助函数则反查**一层**调用方，
    由调用方的路由装饰器反推受影响接口（`_record_thread_activity` → thread 路由）。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []
    if not ranges:
        ranges = [(1, max(len(text.splitlines()), 1))]
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    route_of: dict[str, str] = {}
    for fn in funcs:
        route = _route_of_decorators(fn)
        if route:
            route_of[fn.name] = route
    callers = _callers_index(tree)
    apis: list[str] = []
    symbols: list[str] = []
    for fn in funcs:
        if not _fn_hit(fn, ranges):
            continue
        symbols.append(fn.name)
        direct = route_of.get(fn.name)
        if direct:
            _add_unique(apis, [direct])
            continue
        for caller in sorted(callers.get(fn.name, ())):
            if caller in route_of:
                _add_unique(apis, [route_of[caller]])
    return apis, symbols


def _file_kind(rel: str) -> str:
    """变更文件分类：决定用哪条提取链路。"""
    if _is_test_file(rel):
        return "test"
    if rel.lower().endswith(".py"):
        return "py"
    if _is_api_layer_file(rel):
        # 用 "api_layer" 而非 "api"：避免与 core.enums 里取值为 "api" 的枚举碰撞
        # （check_enums 门禁把 return 裸字面量当违规）；仅内部 handler key，语义不变。
        return "api_layer"
    if _is_page_file(rel):
        return "shell" if _is_shell_file(rel) else "page"
    if rel.lower().endswith(_TS_EXT):
        return "ts"
    return "other"


def _facts_backend(
    facts: _ChangedFacts, rel: str, text: str, ranges: Sequence[tuple[int, int]]
) -> bool:
    apis, symbols = _backend_facts(text, ranges)
    _add_unique(facts.api_literals, apis)
    _add_unique(facts.symbols, symbols)
    return bool(apis or symbols)


def _facts_api_layer(
    facts: _ChangedFacts, rel: str, text: str, ranges: Sequence[tuple[int, int]]
) -> bool:
    literals = _api_literals_in(text, ranges)
    _add_unique(facts.api_literals, literals)
    _add_unique(facts.symbols, _ts_symbols_near(text, ranges))
    return bool(literals)


def _facts_page(
    facts: _ChangedFacts, rel: str, text: str, ranges: Sequence[tuple[int, int]]
) -> bool:
    cands = _page_candidates_from_name(rel)
    _add_unique(facts.page_candidates, cands)
    # 仅取**改动区域附近**的路由字面量（如新增的 navigate('/chat')），不是整文件。
    _add_unique(facts.route_literals, _route_literals_in(_slice_by_ranges(text, ranges)))
    _add_unique(facts.symbols, _ts_symbols_near(text, ranges))
    return bool(cands or facts.route_literals)


def _facts_shell(
    facts: _ChangedFacts, rel: str, text: str, ranges: Sequence[tuple[int, int]]
) -> bool:
    """壳/布局文件：路由表是全站菜单，只留符号关键词，不产出页面目标。"""
    _add_unique(facts.symbols, _ts_symbols_near(text, ranges))
    return True


def _facts_ts(facts: _ChangedFacts, rel: str, text: str, ranges: Sequence[tuple[int, int]]) -> bool:
    """其他前端文件（store/utils/mock）：只贡献符号关键词，不参与页面目标判定。"""
    _add_unique(facts.symbols, _ts_symbols_near(text, ranges))
    return bool(facts.symbols)


def _facts_ignored(
    facts: _ChangedFacts, rel: str, text: str, ranges: Sequence[tuple[int, int]]
) -> bool:
    """测试文件：import 的是全站接口，拿它当依据等于回到全站巡检，直接忽略。"""
    return True


_FILE_HANDLERS = {
    "test": _facts_ignored,
    "py": _facts_backend,
    "api_layer": _facts_api_layer,
    "page": _facts_page,
    "shell": _facts_shell,
    "ts": _facts_ts,
}


def _apply_file_facts(
    facts: _ChangedFacts, rel: str, text: str, ranges: Sequence[tuple[int, int]]
) -> bool:
    """把一个变更文件的事实并入 `facts`；返回是否产出了可用信息（False → 记未映射）。"""
    handler = _FILE_HANDLERS.get(_file_kind(rel))
    return handler(facts, rel, text, ranges) if handler else False


def _collect_facts(
    changed_files: Iterable[str],
    hunks: Mapping[str, Sequence[tuple[int, int]]],
    sources: Mapping[str, Any],
) -> _ChangedFacts:
    facts = _ChangedFacts()
    for raw in changed_files:
        rel = normalize_rel(raw)
        if not rel:
            continue
        text = _text_of(sources.get(rel))
        ranges = _hunk_ranges(hunks, rel)
        if not ranges and text:
            # 拿不到该文件的 hunk 区间 → 只能全文扫描，容易偏宽，记录下来供排障。
            facts.no_hunk.append(rel)
        if not _apply_file_facts(facts, rel, text, ranges):
            facts.unmatched.append(rel)
    # 页面文件里提取到的路由字面量作为候选（弱于文件名推断，仍限于改动区域）
    _add_unique(facts.page_candidates, facts.route_literals)
    return facts


_TS_FN_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*="
)


def _ts_symbols_near(text: str, ranges: Sequence[tuple[int, int]]) -> list[str]:
    """按 hunk 区域切片后取函数/常量名（TS 无 AST，用行首声明正则近似）。"""
    chunk = _slice_by_ranges(text, ranges)
    out: list[str] = []
    for line in chunk.splitlines():
        m = _TS_FN_RE.search(line)
        if m:
            name = m.group(1) or m.group(2)
            if name and name not in out:
                out.append(name)
    return out


# ============================================================================
# 与运行时清单比对
# ============================================================================
def _kw_hits_path(keyword: str, path: str) -> bool:
    """段级关键词匹配：`thread` 命中 `/api/v1/threads`，但不命中 `/threading-x`。"""
    for seg in (path or "").lower().strip("/").split("/"):
        if not seg:
            continue
        if seg == keyword or seg in (keyword + "s", keyword + "es"):
            return True
        if seg.startswith(keyword) and seg[len(keyword) :] in ("s", "es", "-list", "-page"):
            return True
    return False


def _pick_api_targets(
    plan: TargetPlan, endpoints: Sequence[tuple[str, str]], facts: _ChangedFacts
) -> None:
    keywords = plan.keywords
    for _method, raw in endpoints:
        path = normalize_api_path(raw)
        if not path or path in plan.api_paths:
            continue
        if facts.api_literals:
            hit = next((lit for lit in facts.api_literals if _path_covers(lit, path)), "")
            if hit:
                plan.api_paths.append(path)
                plan.reasons[path] = f"变更代码里的接口字面量 {hit}"
            continue
        if keywords:
            kw = next((k for k in keywords if _kw_hits_path(k, path)), "")
            if kw:
                plan.api_paths.append(path)
                plan.reasons[path] = f"变更符号关键词「{kw}」（弱证据）"


def _pick_page_targets(plan: TargetPlan, pages: Sequence[str], facts: _ChangedFacts) -> None:
    for raw in pages:
        page = _normalize_page_path(raw)
        if page in plan.page_paths:
            continue
        hit = next((c for c in facts.page_candidates if _page_covers(c, page)), "")
        if hit:
            plan.page_paths.append(page)
            plan.reasons[page] = f"变更页面文件推断的路由 {hit}"


def build_target_plan(
    *,
    changed_files: Iterable[str],
    hunks: Mapping[str, Sequence[tuple[int, int]]] | None = None,
    sources: Mapping[str, Any] | None = None,
    api_endpoints: Sequence[tuple[str, str]] = (),
    page_paths: Sequence[str] = (),
) -> TargetPlan:
    """构造靶向计划。

    参数：
        changed_files: 变更文件相对路径（正/反斜杠均可）。
        hunks: `{rel: [(start, len), ...]}`，用于把分析范围收敛到改动行附近。
        sources: `{rel: SourceFile | str}`，变更文件内容；缺失时按「无文本」处理（只能靠文件名）。
        api_endpoints: 运行时实际拦截到的 `[(method, path), ...]`，用于**只保留真实存在的目标**。
        page_paths: 运行时实际发现的可达页面路径。

    返回 `TargetPlan`；`is_empty=True` 表示本次变更没有任何可映射的运行时目标。
    """
    facts = _collect_facts(changed_files, dict(hunks or {}), dict(sources or {}))
    plan = TargetPlan(keywords=_keywords(facts.symbols), symbols=list(facts.symbols))
    plan.unmatched_files = list(facts.unmatched)
    _pick_api_targets(plan, list(api_endpoints), facts)
    _pick_page_targets(plan, list(page_paths), facts)
    if not hunks:
        # hunk 缺失时只能全文扫描字面量，容易把整站接口都算进来——如实提示，便于排障。
        plan.notes.append(
            "变更靶向：没有可用的 diff hunk，按文件全文提取接口/路由字面量（可能偏宽）"
        )
    elif facts.no_hunk:
        plan.notes.append(
            f"变更靶向：{len(facts.no_hunk)} 个变更文件没有 hunk 区间，按全文提取（可能偏宽）："
            + "、".join(facts.no_hunk[:4])
        )
    if plan.is_empty:
        plan.notes.append(
            "变更靶向：本次变更未命中任何运行时接口/页面（改了样式、配置或未映射到路由的代码），"
            "按约定不产出 URL 用例"
        )
    else:
        plan.notes.append(
            f"变更靶向：命中 {len(plan.page_paths)} 个页面、{len(plan.api_paths)} 个接口"
            f"（依据 {len(facts.api_literals)} 条接口字面量 / {len(facts.page_candidates)} 个路由候选）"
        )
    if facts.unmatched:
        plan.notes.append(
            f"变更靶向：{len(facts.unmatched)} 个变更文件没有可映射目标："
            + "、".join(facts.unmatched[:6])
            + ("…" if len(facts.unmatched) > 6 else "")
        )
    return plan
