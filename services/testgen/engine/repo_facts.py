"""引擎 · 仓库级事实（T4）：静态路由表 + 前端 import 边。

**为什么需要它**

1. React/Vue 仓的路由通常**不在页面文件里声明**，而是集中在 `App.tsx` / `router.ts`：
   `pages/pc/PcChatPage.tsx` 里没有任何 `path:`，功能点只能标 `has_route=False`，
   于是「页面可达 / 未授权访问 / 极端视口」这些维度一条也展开不了（只剩「交互元素可用」）。
2. 前端是 **import 图**而不是调用图：`impact.py` 的同文件反向调用图管不到
   「父页面 import 了被改的子模块」。改一个被三处复用的组件，三处父页面全裸奔。

本模块只做两件纯计算的事（不调 LLM、不写库、不改任何功能点）：

- **静态路由表**：从 `<Route path="/x" element={<Page />} />`、
  `{ path: '/x', component: Page }` 抽出「组件名 → 路由」绑定；
- **import 边**：解析前端文件的 import 说明符（相对路径 `./x`、别名 `@/a/b`），
  只保留「目标 = 本次变更文件」的边，供文件级跨文件回归兜底消费。

**保守原则**（宁可少报，不编造）

- 路由必须被路由表**印证**（组件名或文件名候选与实际声明双向匹配）才推断，
  绝不「拿文件名当路由」——那正是历史缺陷（121/145 个假路径）的成因；
- 只认 router-ish 文件里的声明（含 `<Route` / `createBrowserRouter` / `useRoutes`），
  避免把 mock 数据、表单配置里的 `path:` 当成路由；
- import 说明符解析不出真实文件（第三方包、路径别名无共同后缀）就不产边。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.log import get_logger


log = get_logger(__name__)

# 前端文件扩展名（与 scan.FRONTEND_EXTS 同口径；此处只用于本模块内部判定）
_FRONT_EXTS: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".vue")

# router-ish 判定：只有这些文件里的 `path:` 才当路由声明（避免 mock/表单误判）
_ROUTER_HINTS = ("<Route", "<route", "createBrowserRouter", "createRouter", "useRoutes")
# 路由表常见文件名片段（用于限定扫描面，进一步降噪）
_ROUTER_NAME_HINTS = ("app", "router", "routes", "route", "main")

_ROUTE_TAG_RE = re.compile(r"<Route\b")
_ATTR_PATH_RE = re.compile(r"""\bpath\s*=\s*['"]([^'"]+)['"]""")
_ATTR_ELEMENT_RE = re.compile(r"element\s*=\s*\{([^{}]*)\}", re.S)
_ATTR_COMPONENT_RE = re.compile(r"\bcomponent\s*[=:]\s*\{?\s*([A-Z][\w]*)")
_JSX_TAG_RE = re.compile(r"<([A-Z][\w]*)")

_OBJ_PATH_RE = re.compile(r"""\bpath\s*:\s*['"]([^'"]+)['"]""")
_OBJ_COMPONENT_RE = re.compile(r"\bcomponent\s*:\s*([A-Z][\w]*)")
_OBJ_IMPORT_RE = re.compile(
    r"""\b(?:component|lazy)\s*:\s*\(?\s*\)?\s*=>\s*import\s*\(\s*['"]([^'"]+)['"]"""
)

_IMPORT_FROM_RE = re.compile(r"""import\s+([\s\S]{0,300}?)\s+from\s+['"]([^'"]+)['"]""")
_IMPORT_SIDE_RE = re.compile(r"""^import\s+['"]([^'"]+)['"]""", re.M)
_IMPORT_DYNAMIC_RE = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_BRACE_BLOCK_RE = re.compile(r"\{([\s\S]*?)\}")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")

# 路由参数段：`:id?` / `:threadId` / `*` —— 归一化时整段丢弃（用例地址只到稳定前缀）
_PARAM_SEG_RE = re.compile(r"^[:*]")
# 单个文件的扫描上限（防止超长压缩产物拖死生成）
_MAX_TEXT_CHARS = 400_000
# 参与 import 边解析的前端文件上限（超出的按路径排序截断，并在 stats 里如实上报）
_MAX_IMPORT_FILES = 3000


def normalize_route(path: str) -> str:
    """路由归一化：去查询串/尾斜杠/动态参数段，`/chat/:id?` → `/chat`。

    全是参数段的路由（`/:id`、`/*`）返回空串——那类声明无法给出稳定前缀，
    当作「没有可用的路由证据」，避免把它当成根路由 `/` 误绑到首页。
    """
    raw = str(path or "").strip().split("?")[0].split("#")[0]
    if not raw:
        return ""
    segs = [s for s in raw.replace("\\", "/").split("/") if s]
    if not segs:
        return "/"
    kept = [s for s in segs if not _PARAM_SEG_RE.match(s)]
    if not kept:
        return ""
    return "/" + "/".join(kept)


def _norm_no_ext(rel: str) -> str:
    """相对路径去掉前端扩展名（供 import 说明符后缀比对）。"""
    p = str(rel or "").replace("\\", "/")
    for ext in _FRONT_EXTS:
        if p.lower().endswith(ext):
            return p[: -len(ext)]
    return p


def _strip_ext(spec: str) -> str:
    """说明符去掉扩展名（`./PcShell.tsx` 与 `./PcShell` 等价）。"""
    for ext in _FRONT_EXTS:
        if spec.lower().endswith(ext):
            return spec[: -len(ext)]
    return spec


def is_frontend(rel: str) -> bool:
    return str(rel or "").lower().endswith(_FRONT_EXTS)


def is_routerish(rel: str, text: str) -> bool:
    """是否为「声明路由」的文件：内容含路由 API，或文件名像路由入口。"""
    head = text[:_MAX_TEXT_CHARS]
    if any(hint in head for hint in _ROUTER_HINTS):
        return True
    stem = Path(rel).stem.lower()
    return any(hint == stem or stem.endswith(hint) for hint in _ROUTER_NAME_HINTS)


@dataclass(frozen=True)
class RouteBinding:
    """一条「组件 → 路由」绑定（来自路由声明，已归一化）。"""

    component: str  # 组件名（JSX 标签名 / import 名）
    path: str  # 归一化后的路由（`/pc/chat`）
    source: str  # 路由声明所在文件
    raw: str = ""  # 原始 path 字面量（含参数段），用于展示「依据」
    file: str = ""  # 组件所在源文件（由同文件 import 别名/动态 import 解析，只增不改）

    def evidence(self) -> str:
        base = (
            f"{self.source} 声明 {self.raw or self.path} → 组件 {self.component}"
            if self.component
            else f"{self.source} 声明 {self.raw or self.path}"
        )
        return f"{base}（组件文件 {self.file}）" if self.file else base


@dataclass(frozen=True)
class ImportSpec:
    """一条 import 说明符（已尽量解析成仓库内相对路径）。"""

    importer: str  # 导入方文件
    spec: str  # 原始说明符（`@/pages/pc/PcChatPage`）
    symbols: tuple[str, ...] = ()  # 具名导入的符号（`{ PcComposer }` → ("PcComposer",)）
    resolved: str = ""  # 相对路径说明符解析结果（去扩展名）；别名/包为空

    def to_dict(self) -> dict[str, Any]:
        return {
            "importer": self.importer,
            "spec": self.spec,
            "symbols": list(self.symbols),
            "resolved": self.resolved,
        }


@dataclass
class RepoFacts:
    """仓库级事实快照（扫描阶段一次性构建，随 PipelineResult 传递）。"""

    routes: list[RouteBinding] = field(default_factory=list)
    imports: dict[str, list[ImportSpec]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)

    def route_of_component(self, component: str) -> RouteBinding | None:
        """按组件名查路由（同名多条时取最短路由，结果稳定）。"""
        name = (component or "").strip().lower()
        if not name:
            return None
        hits = [b for b in self.routes if b.component.lower() == name]
        return min(hits, key=lambda b: (len(b.path), b.path)) if hits else None

    def routes_of_path(self, path: str) -> list[RouteBinding]:
        target = normalize_route(path)
        return [b for b in self.routes if b.path == target]

    def route_of_file(self, rel: str) -> RouteBinding | None:
        """按**组件源文件**反查路由（别名绑定优先于组件名：同名 Dashboard 不会串页）。

        只有 `RouteBinding.file` 非空时才会命中——没有 import 证据时宁可返回 None，
        也不能靠文件名猜测。
        """
        target = _norm_no_ext(rel)
        if not target:
            return None
        hits = [b for b in self.routes if b.file and _norm_no_ext(b.file) == target]
        return min(hits, key=lambda b: (len(b.path), b.path)) if hits else None

    def importers_of(self, rel: str, *, exclude_router: bool = True) -> list[ImportSpec]:
        """哪些前端文件 import 了 `rel`（只返回能静态解析到该文件的边）。"""
        target = _norm_no_ext(rel)
        if not target:
            return []
        out: list[ImportSpec] = []
        for importer, specs in self.imports.items():
            if importer == rel:
                continue
            for spec in specs:
                if not _spec_hits(spec, target):
                    continue
                if exclude_router and _is_router_file(importer):
                    continue
                out.append(spec)
        return sorted(out, key=lambda s: (s.importer, s.spec))

    def to_dict(self) -> dict[str, Any]:
        return {
            "routes": [
                {
                    "component": r.component,
                    "path": r.path,
                    "source": r.source,
                    "raw": r.raw,
                    "file": r.file,
                }
                for r in self.routes
            ],
            "import_files": len(self.imports),
            "stats": dict(self.stats),
        }


# ============================================================================
# 说明符解析
# ============================================================================
def _spec_hits(spec: ImportSpec, target_norm: str) -> bool:
    """说明符是否指向 `target_norm`（去掉扩展名的仓库相对路径）。"""
    if spec.resolved and spec.resolved == target_norm:
        return True
    raw = _strip_ext(spec.spec.strip())
    if not raw:
        return False
    # 别名（`@/pages/pc/PcChatPage`、`~/pages/...`、`/src/...`）→ 按段对齐的后缀匹配
    tail = raw.lstrip("@~").lstrip("/")
    if not tail or tail.startswith("."):
        return False
    return target_norm.endswith("/" + tail)


def _is_router_file(rel: str) -> bool:
    """导入方是否路由/壳文件：它们的「引用」就是路由表本身，已由路由推断覆盖。"""
    stem = Path(rel).stem.lower()
    if any(hint == stem for hint in ("app", "router", "routes", "main")):
        return True
    return any(hint in stem for hint in ("router", "routes"))


def _symbols_of(clause: str) -> tuple[str, ...]:
    """从 `import X, { A, B as C } from ...` 的左侧子句里取符号名。"""
    names: list[str] = []
    for block in _BRACE_BLOCK_RE.findall(clause or ""):
        for piece in block.split(","):
            token = piece.strip().split(" as ")[-1].strip()
            if _IDENT_RE.fullmatch(token or "") and token not in names:
                names.append(token)
    default = (clause or "").split(",")[0].strip()
    if _IDENT_RE.fullmatch(default or "") and default not in names:
        names.insert(0, default)
    return tuple(names)


def _resolve_spec(spec: str, importer: str) -> str:
    """相对路径说明符 → 仓库内相对路径（去扩展名）；别名/包返回空串。"""
    if not spec.startswith("."):
        return ""
    base = posixpath.dirname(str(importer).replace("\\", "/"))
    joined = posixpath.normpath(posixpath.join(base, spec))
    if joined.startswith(".."):
        return ""
    return _strip_ext(joined.lstrip("./"))


def _import_specs_in(rel: str, text: str) -> list[ImportSpec]:
    out: list[ImportSpec] = []
    seen: set[str] = set()
    for m in _IMPORT_FROM_RE.finditer(text):
        spec = m.group(2).strip()
        if not spec or spec in seen:
            continue
        seen.add(spec)
        out.append(
            ImportSpec(
                importer=rel,
                spec=spec,
                symbols=_symbols_of(m.group(1)),
                resolved=_resolve_spec(spec, rel),
            )
        )
    for regex in (_IMPORT_SIDE_RE, _IMPORT_DYNAMIC_RE):
        for m in regex.finditer(text):
            spec = m.group(1).strip()
            if not spec or spec in seen:
                continue
            seen.add(spec)
            out.append(ImportSpec(importer=rel, spec=spec, resolved=_resolve_spec(spec, rel)))
    return out


# ============================================================================
# import 别名 → 组件文件（批次 B）：让路由绑定落到真实文件，而不是只靠组件名
# ============================================================================
def _resolve_spec_to_file(spec: str, importer: str, files_norm: dict[str, str]) -> str:
    """把 import 说明符解析成 `files` 里的真实前端文件路径；解析不出返回空串。

    支持相对路径与常见别名（`@/views/X.vue`、`~/views/X`、`/src/views/X`）。
    **只接受实际存在的文件**：这是「不编造路径」的底线——别名解析不出就不产绑定，
    宁可保持无路由，也不拿候选路径冒充证据。
    """
    spec = str(spec or "").strip()
    candidates: list[str] = []
    if spec.startswith("."):
        base = posixpath.dirname(str(importer).replace("\\", "/"))
        candidates.append(posixpath.normpath(posixpath.join(base, spec)))
    else:
        tail = spec.lstrip("@~").lstrip("/")
        if tail:
            candidates.extend((tail, f"src/{tail}"))
    for cand in candidates:
        norm = _strip_ext(cand.lstrip("./"))
        found = files_norm.get(norm)
        if found:
            return found
    return ""


def _alias_map(rel: str, text: str, files_norm: dict[str, str]) -> dict[str, str]:
    """路由文件里的 `import X from ...` → `{X: 真实文件}`。"""
    out: dict[str, str] = {}
    for m in _IMPORT_FROM_RE.finditer(text):
        target = _resolve_spec_to_file(m.group(2), rel, files_norm)
        if not target:
            continue
        for name in _symbols_of(m.group(1)):
            out[name] = target
    return out


# ============================================================================
# 路由表解析
# ============================================================================
def _is_absolute_route(raw: str) -> bool:
    """只接受绝对路由声明（`/pc/daily-perf`）。

    相对子路由（`<Route path="daily-perf">` 嵌在 `path="/pc"` 下）需要完整的嵌套前缀
    栈才能还原真实地址，正则层做不到；用它当用例地址会得到 `/daily-perf` 这种
    不存在的路径（实测 App.tsx 里两种写法并存）。因此相对声明一律不做路由证据，
    宁可让该页面保持「无路由」口径，也不写错地址。
    """
    text = str(raw or "").strip()
    return text.startswith("/") and not text.startswith("//")


def _route_bindings_jsx(
    rel: str, text: str, alias: dict[str, str] | None = None
) -> list[RouteBinding]:
    """JSX `<Route>` 声明：按标签位置切片，窗口内取 path 与 element 里的组件名。

    不能用一个 `[^>]*` 正则抓属性——`element={<PcWrap ...><Page /></PcWrap>}`
    的 `>` 出现在 element 内部，正则会提前截断，导致嵌套组件丢失（实测
    `/pc/research` 只认出 `PcWrap`、认不出真正渲染的 `MobileInvestHomePage`）。
    """
    starts = [m.start() for m in _ROUTE_TAG_RE.finditer(text)]
    out: list[RouteBinding] = []
    for i, pos in enumerate(starts):
        seg_end = starts[i + 1] if i + 1 < len(starts) else min(len(text), pos + 600)
        segment = text[pos:seg_end]
        path_m = _ATTR_PATH_RE.search(segment)
        if not path_m:
            continue  # 布局路由（无 path）：不产出绑定
        raw = path_m.group(1)
        if not _is_absolute_route(raw):
            continue
        normalized = normalize_route(raw)
        if not normalized:
            continue
        components: list[str] = []
        element_m = _ATTR_ELEMENT_RE.search(segment)
        if element_m:
            components.extend(_JSX_TAG_RE.findall(element_m.group(1)))
        component_m = _ATTR_COMPONENT_RE.search(segment)
        if component_m:
            components.append(component_m.group(1))
        # element 里可能再套一层 `component: X`；窗口内的显式声明一并收下
        alias_map = alias or {}
        for name in components:
            out.append(
                RouteBinding(
                    component=name,
                    path=normalized,
                    source=rel,
                    raw=raw,
                    file=alias_map.get(name, ""),
                )
            )
    return out


def _route_bindings_object(
    rel: str,
    text: str,
    files_norm: dict[str, str] | None = None,
    alias: dict[str, str] | None = None,
) -> list[RouteBinding]:
    """对象式路由表（`{ path: '/x', component: Page }` / `lazy: () => import(...)`）。

    **窗口必须截到下一条 `path:`**：旧实现固定向后看 240 字符，`/ai-generation/assistant`
    自己的组件是 `() => import(...)`（正则 `component: X` 不认），却搜到了**下一条**路由
    的 `component: Layout` —— 实测 130 条 path 只产出 18 条绑定，且部分组件名是错的。
    现在先按 `path:` 边界截断，再取窗口内**位置最早**的组件声明。
    """
    out: list[RouteBinding] = []
    files_norm = files_norm or {}
    alias_map = alias or {}
    for m in _OBJ_PATH_RE.finditer(text):
        if not _is_absolute_route(m.group(1)):
            continue
        normalized = normalize_route(m.group(1))
        if not normalized:
            continue
        next_m = _OBJ_PATH_RE.search(text, m.end())
        end = min(m.start() + 240, next_m.start() if next_m else len(text))
        window = text[m.start() : end]
        comp_m = _OBJ_COMPONENT_RE.search(window)
        imp_m = _OBJ_IMPORT_RE.search(window)
        if comp_m and (imp_m is None or comp_m.start() < imp_m.start()):
            name = comp_m.group(1)
            out.append(
                RouteBinding(
                    component=name,
                    path=normalized,
                    source=rel,
                    raw=m.group(1),
                    file=alias_map.get(name, ""),
                )
            )
        elif imp_m is not None:
            spec = imp_m.group(1)
            stem = Path(_strip_ext(spec)).name
            out.append(
                RouteBinding(
                    component=stem,
                    path=normalized,
                    source=rel,
                    raw=m.group(1),
                    file=_resolve_spec_to_file(spec, rel, files_norm),
                )
            )
    return out


def _object_spans(text: str) -> list[tuple[int, int]]:
    """扫描所有 `{...}` 对象边界（忽略字符串里的花括号）。

    对象式路由表靠缩进/花括号嵌套表达 `children`，正则看不到层级；
    先拿到每个对象的起止位置，才能把相对子路由 `path: 'dashboard'` 还原成
    `/api-testing/dashboard`。
    """
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    quote = ""
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            spans.append((stack.pop(), i))
    return sorted(spans)


def _route_bindings_nested(  # noqa: C901 - 一次线性扫描同时算对象边界/父前缀/组件，拆开会重扫三遍
    rel: str,
    text: str,
    files_norm: dict[str, str] | None = None,
    alias: dict[str, str] | None = None,
) -> list[RouteBinding]:
    """对象式 `children` 相对子路由：按父前缀拼出真实地址。

    T4 曾把相对 `path:` 一律丢弃（没有父前缀，写出来的地址会是错的）。这里用花括号
    嵌套关系**还原父前缀**，因此既不再编造 `/dashboard`，也不再漏掉整棵子路由树——
    实测本仓 130 条 `path:` 里绝大多数是相对子路由，正是 109 条文件级用例的来源。
    """
    files_norm = files_norm or {}
    alias_map = alias or {}
    spans = _object_spans(text)
    own: dict[tuple[int, int], str] = {}
    for s, e in spans:
        chars = list(text[s + 1 : e])
        for a, b in spans:
            if s < a and b < e:
                for i in range(a - s, min(b - s + 1, len(chars))):
                    chars[i] = " "
        own[(s, e)] = "".join(chars)

    parents: dict[tuple[int, int], tuple[int, int]] = {}
    for span in spans:
        enclosing = [p for p in spans if p[0] < span[0] and span[1] < p[1]]
        if enclosing:
            parents[span] = min(enclosing, key=lambda p: p[1] - p[0])

    full_cache: dict[tuple[int, int], str] = {}

    def full_of(span: tuple[int, int]) -> str:
        if span in full_cache:
            return full_cache[span]
        raw_path_m = _OBJ_PATH_RE.search(own[span])
        if not raw_path_m:
            return ""
        raw = raw_path_m.group(1)
        parent = parents.get(span)
        base = full_of(parent) if parent else ""
        if raw.startswith("/"):
            full = normalize_route(raw)
        elif base:
            full = normalize_route(posixpath.join(base, raw))
        else:
            full = ""  # 相对声明无父前缀：不猜地址（与 T4 保守原则一致）
        full_cache[span] = full
        return full

    out: list[RouteBinding] = []
    for span in spans:
        full = full_of(span)
        if not full:
            continue
        path_m = _OBJ_PATH_RE.search(own[span])
        if path_m is None:
            continue
        raw = path_m.group(1)
        comp_m = _OBJ_COMPONENT_RE.search(own[span])
        imp_m = _OBJ_IMPORT_RE.search(own[span])
        if comp_m and (imp_m is None or comp_m.start() < imp_m.start()):
            name = comp_m.group(1)
            out.append(
                RouteBinding(
                    component=name,
                    path=full,
                    source=rel,
                    raw=raw,
                    file=alias_map.get(name, ""),
                )
            )
        elif imp_m is not None:
            spec = imp_m.group(1)
            out.append(
                RouteBinding(
                    component=Path(_strip_ext(spec)).name,
                    path=full,
                    source=rel,
                    raw=raw,
                    file=_resolve_spec_to_file(spec, rel, files_norm),
                )
            )
    return out


def _route_bindings_in(
    rel: str, text: str, files_norm: dict[str, str] | None = None
) -> list[RouteBinding]:
    if not is_routerish(rel, text):
        return []
    files_norm = files_norm or {}
    alias = _alias_map(rel, text, files_norm)
    bindings = (
        _route_bindings_jsx(rel, text, alias)
        + _route_bindings_object(rel, text, files_norm, alias)
        + _route_bindings_nested(rel, text, files_norm, alias)
    )
    # 去重：同一 (组件, 路由) 只留一条（JSX 与对象两种写法可能都命中同一段文本）
    seen: set[tuple[str, str, str]] = set()
    out: list[RouteBinding] = []
    for b in bindings:
        key = (b.component.lower(), b.path, b.file)
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


# ============================================================================
# 对外入口
# ============================================================================
def build_repo_facts(
    files: dict[str, Any],
    *,
    max_import_files: int = _MAX_IMPORT_FILES,
) -> RepoFacts:
    """从**全量**索引构建仓库事实（必须在 selected_paths 过滤之前调用）。

    `files` 为 `{相对路径: SourceFile}`（可含未勾选文件）——导入方与路由表往往
    不在用户勾选范围内，这正是「勾 5 个文件、影响面却跨文件」的成因。
    """
    facts = RepoFacts()
    # 测试/夹具文件不参与：它们 import 全站（`round4-harness.tsx` 引了 PcChatPage），
    # 拿它当「受影响父组件」等于又回到全站巡检。
    front: list[str] = [
        rel
        for rel in sorted(files)
        if is_frontend(rel) and not bool(getattr(files.get(rel), "is_noise", False))
    ]
    truncated = max(0, len(front) - max(1, max_import_files))
    front = front[: max(1, max_import_files)]
    # 别名解析只接受真实存在的文件：`files_norm` 是「去重后的相对路径 → 原路径」索引。
    files_norm: dict[str, str] = {}
    for rel in front:
        files_norm.setdefault(_norm_no_ext(rel), rel)

    route_files = 0
    for rel in front:
        sf = files.get(rel)
        text = getattr(sf, "text", "") or ""
        if not text:
            continue
        bindings = _route_bindings_in(rel, text, files_norm)
        if bindings:
            route_files += 1
            facts.routes.extend(bindings)
        specs = _import_specs_in(rel, text[:_MAX_TEXT_CHARS])
        if specs:
            facts.imports[rel] = specs

    facts.stats = {
        "repo_front_files": len(front),
        "repo_route_files": route_files,
        "repo_route_bindings": len(facts.routes),
        "repo_route_file_bindings": sum(1 for b in facts.routes if b.file),
        "repo_import_files": len(facts.imports),
        "repo_import_specs": sum(len(v) for v in facts.imports.values()),
        "repo_import_truncated": truncated,
    }
    log.info(
        "仓库事实构建完成",
        extra={
            "routes": len(facts.routes),
            "imports": facts.stats["repo_import_specs"],
            "truncated": truncated,
        },
    )
    return facts


def infer_route(  # noqa: PLR0911 - 五条证据链逐条早退，比嵌套条件更好读
    rel: str,
    facts: RepoFacts,
    *,
    runtime_pages: list[str] | tuple[str, ...] = (),
) -> RouteBinding | None:
    """给「位于 pages/ 但没有 path 声明」的文件反查真实路由（推断，非编造）。

    三条证据链，按可信度排序：
    1. **组件名精确绑定**：路由表里 `element={<PcChatPage />}` 与文件 stem 同名；
    2. **文件名候选 vs 路由表**：`PcChatPage` → `/pc/chat`、`/chat` 命中已声明路由；
    3. **运行时页面**（本次启用运行时发现时才有）：候选前缀匹配。
    都不命中返回 None——调用方保持 `has_route=False`，只展开交互/语义维度。
    """
    if not rel or not is_frontend(rel):
        return None
    # 批次 B：路由表 import 别名先把组件绑到**具体文件**，优先级高于组件名——
    # 本仓有 6 个 Dashboard.vue，靠 stem 会把 api-testing 的页面认成 monitor 的路由。
    by_file = facts.route_of_file(rel)
    if by_file is not None:
        return by_file
    stem = Path(rel).stem
    exact = facts.route_of_component(stem)
    if exact is not None:
        return exact

    from engine.change_targeting import _page_candidates_from_name

    candidates = _page_candidates_from_name(rel)
    if not candidates:
        return None
    for cand in candidates:
        hits = [
            b for b in facts.routes if b.path == cand or b.path.startswith(cand.rstrip("/") + "/")
        ]
        if hits:
            return min(hits, key=lambda b: (len(b.path), b.path))
    for cand in candidates:
        for page in runtime_pages:
            normalized = normalize_route(page)
            if normalized and (normalized == cand or normalized.startswith(cand.rstrip("/") + "/")):
                # 用**候选前缀**而不是具体 URL 作地址：`/pc/chat/123` 是某次访问的实例，
                # 写成用例地址就会依赖一条具体记录；`/pc/chat` 才是可复用的路由。
                return RouteBinding(component=stem, path=cand, source="运行时页面", raw=str(page))
    return None
