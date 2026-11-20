"""引擎 · T3 变更语义识别（change_semantics）。

问题：测试点维度由「来源类型」决定（API → 可用性/鉴权/参数，页面 → 交互元素可用），
于是「这次改的是分页游标」和「改的是按钮文案」产出的用例一模一样——一律
「交互元素可用」，用户看不出测的是什么、执行器也判定不了。

做法：从 diff hunk 的**新增行**里识别 8 类变更语义，再映射到具体维度
（见 `_TAG_DIMS`），交给 `tp_expand` 与类型维度合并。只做可解释、可回滚的关键词
规则，不做语义模型推断（成本高、结果不稳定、无法解释）：

- **只扫 hunk 行**（不扫整文件）→ 没改的部分不产生语义；
- **排除注释**（`#` / `//` 整行注释 + `/* … */` 块注释跨行状态）→ 注释里提到
  `offset`/分页不算改了分页（实测块注释的折行会让注释内容被当成变更）；
- **每个语义至少命中 2 个不同关键词，且其中 1 个是强信号**
  （`_STRONG`）→ 单词 `mode` 在代码里极常见，只凭 1 个词就判「分支模式」会大面积误判；
- 同一段改动命中多个语义时按 `_TAG_ORDER` 排序，由调用方按预算截断（维度上限 4）。

为什么保留字符串字面量里的关键词：本次改动的语义常常**就是**文案/字段名本身
（如「标题兜底为『未命名对话』」「字段 title 形状变化」），把它们排除会漏掉最关键的
契约与兜底语义；靠「≥2 个信号 + 强信号」来控误匹配，而不是靠过滤字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.enums import Dimension, TPType


# 8 类变更语义（取值即报告/产物里的语义名，只增不改）
TAG_PAGINATION = "pagination"
TAG_DEDUPE = "dedupe"
TAG_LIST_MUTATION = "list_mutation"
TAG_BRANCH_MODE = "branch_mode"
TAG_CONTRACT_SHAPE = "contract_shape"
TAG_FALLBACK = "fallback"
TAG_GUARD_NEGATIVE = "guard_negative"
TAG_AUTH_TOUCH = "auth_touch"

# 语义标签的规范顺序（= 抢维度预算的优先级）：
# 本次实测的改动是「会话列表新增 + 分页游标同步 + 去重」，分页/去重排最前；
# 契约形状/兜底/守卫次之；鉴权排最后（多数页面改动并不涉及）。
_TAG_ORDER: tuple[str, ...] = (
    TAG_PAGINATION,
    TAG_DEDUPE,
    TAG_LIST_MUTATION,
    TAG_BRANCH_MODE,
    TAG_CONTRACT_SHAPE,
    TAG_FALLBACK,
    TAG_GUARD_NEGATIVE,
    TAG_AUTH_TOUCH,
)

# 通用强信号（命中即可作为「确有该语义」的依据）
_STRONG: dict[str, tuple[str, ...]] = {
    TAG_PAGINATION: (
        "offset",
        "fetchedcount",
        "hasmore",
        "loadmore",
        "pagesize",
        "page_size",
        "cursor",
        "分页",
        "翻页",
        "续页",
    ),
    TAG_DEDUPE: (
        "dedupe",
        "unique",
        "seen",
        ".some(",
        "includes(",
        "indexof(",
        "去重",
        "重复",
        "same id",
    ),
    TAG_LIST_MUTATION: (
        ".push(",
        ".splice(",
        ".unshift(",
        ".append(",
        ".extend(",
        "insert(",
        "新增一条",
        "数组",
        "列表",
    ),
    TAG_BRANCH_MODE: (
        # 不用裸 "mode"：它是 "model" 的子串（LLM 相关代码里遍地都是），
        # 会把「接了个新模型」误判成「分支模式改动」。只认带分隔符/大小写组合的写法。
        "mode:",
        "mode =",
        "mode==",
        "mode!=",
        "chatmode",
        "sessionmode",
        "setmode(",
        "switch (",
        "switch(",
        "variant",
        "分支",
        "模式",
        "渠道",
    ),
    TAG_CONTRACT_SHAPE: (
        "schema",
        "serialize",
        "to_dict",
        "字段",
        "返回结构",
        "contract",
    ),
    TAG_FALLBACK: (
        "fallback",
        "||",
        "??",
        "catch ",
        " or ",
        "兜底",
        "降级",
        "未命名",
        "默认值",
    ),
    TAG_GUARD_NEGATIVE: (
        "raise",
        "throw",
        "abort",
        "length >",
        "slice(",
        "截断",
        "校验",
        "边界",
    ),
    TAG_AUTH_TOUCH: (
        "authorization",
        "authenticate",
        "auth_",
        "token",
        "permission",
        "鉴权",
        "权限",
        "认证",
        "未登录",
    ),
}

# 通用弱信号（只用于凑「≥2 个证据」，单独出现不成立）
_WEAK: dict[str, tuple[str, ...]] = {
    TAG_PAGINATION: ("page", "limit", "total", "scrol", "滚动"),
    TAG_DEDUPE: ("map(", "set(", ".delete(", ".has(", "item.id"),
    TAG_LIST_MUTATION: ("[...", "...previous", "items", "entries", "previous", "list"),
    TAG_BRANCH_MODE: ("else", "===", "!==", "daily_perf", "office", "channel"),
    TAG_CONTRACT_SHAPE: ("title", "props", "context", "payload", "id:"),
    TAG_FALLBACK: ("else", "null", "undefined", "空串"),
    TAG_GUARD_NEGATIVE: ("none", "null", "undefined", "empty", "空值"),
    TAG_AUTH_TOUCH: ("role", "scope", "login", "user"),
}

# 语言相关的补充信号：前端看 props/context，后端看 dict 字段与 None 判定。
# 分开维护的原因：`props` 在 Python 里几乎不出现、`meta[` 在 TS 里不出现，
# 混在一张表里会让「同一关键词在两种语言里含义不同」的问题被掩盖。
_KIND_EXTRA: dict[str, dict[str, tuple[str, ...]]] = {
    "frontend": {
        TAG_LIST_MUTATION: ("setsessions", "setstate(", "(previous)"),
        TAG_CONTRACT_SHAPE: ("chatsession", "props", "context", "outlet"),
    },
    "backend": {
        TAG_CONTRACT_SHAPE: ("meta[", "payload[", "response["),
        TAG_GUARD_NEGATIVE: ("is not none", "in (none", "is none"),
    },
}

# 语义 → 维度（顺序即优先级）。`auth_touch` 复用既有「安全-鉴权缺失」，
# 不新增同义维度；与既有 plan 重复时由 `tp_expand._merge_semantic` 去重。
_TAG_DIMS: dict[str, tuple[tuple[str, str], ...]] = {
    TAG_PAGINATION: (
        (TPType.BOUNDARY.value, Dimension.PAGINATION_OFFSET_SYNC.value),
        (TPType.NORMAL.value, Dimension.PAGINATION_NO_OVERLAP.value),
    ),
    TAG_DEDUPE: ((TPType.NORMAL.value, Dimension.LIST_DEDUPE.value),),
    TAG_LIST_MUTATION: (
        (TPType.NORMAL.value, Dimension.LIST_INSERT_VISIBLE.value),
        (TPType.NORMAL.value, Dimension.LIST_ORDER_CONSISTENCY.value),
    ),
    TAG_BRANCH_MODE: ((TPType.NORMAL.value, Dimension.BRANCH_ISOLATION.value),),
    TAG_CONTRACT_SHAPE: ((TPType.NORMAL.value, Dimension.CONTRACT_SHAPE.value),),
    TAG_FALLBACK: ((TPType.ABNORMAL.value, Dimension.FALLBACK_DEFAULT.value),),
    TAG_GUARD_NEGATIVE: ((TPType.BOUNDARY.value, Dimension.GUARD_ZERO_VS_EMPTY.value),),
    TAG_AUTH_TOUCH: ((TPType.SECURITY.value, Dimension.AUTH_MISS.value),),
}

# 前端产物扩展名（其余按后端处理）
_FRONTEND_EXTS = (".ts", ".tsx", ".js", ".jsx", ".vue", ".css", ".scss", ".less", ".html")
# 注释前缀：命中即整行丢弃（不改代码语义）。
# `{/*` 是 JSX 注释（React 里成对 `{/* … */}`），漏掉它会把注释**续行**（以中文开头、
# 以 `*/}` 结尾）当成代码——实测它让「…仍按路径重置错误边界」里的「边界」
# 把一次会话列表改动误判成「零值/空值守卫」语义。
_COMMENT_PREFIXES = ("#", "//", "/*", "{/*", "*", "<!--", '"""', "'''")


@dataclass
class SemanticTag:
    """一条命中的变更语义（带证据，便于人工复核误匹配）。"""

    name: str
    keywords: list[str] = field(default_factory=list)
    strong: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "keywords": self.keywords, "strong": self.strong}


def file_kind_of(path: str) -> str:
    """按扩展名判定语言族：`frontend` / `backend`（用于挑选补充信号）。"""
    p = str(path or "").lower().replace("\\", "/")
    return "frontend" if p.endswith(_FRONTEND_EXTS) else "backend"


def is_comment_line(line: str) -> bool:
    """整行注释（含块注释的续行 `*`）——注释里出现关键词不算改了该语义。"""
    return line.strip().startswith(_COMMENT_PREFIXES)


def _starts_inside_block(lines: list[str], begin: int, lookback: int = 20) -> bool:
    """hunk 起点是否落在 `/* … */` 块注释内部（向上看谁先出现：`*/` 在前=在块外）。"""
    for i in range(begin - 1, max(begin - 1 - lookback, -1), -1):
        text = lines[i]
        if "*/" in text:
            return False
        if "/*" in text:
            return True
    return False


def changed_lines(sf: Any, ranges: list[tuple[int, int]], *, max_lines: int = 400) -> list[str]:
    """取源文件在 hunk 行区间内的**有效代码行**（去空行、去注释）。

    块注释要按状态跳过：`/* 第一行` 被前缀规则拦住，但折行的第二行以中文叙述开头、
    前缀规则认不出——实测它把注释里的「分页/新增一条」当成了变更语义。因此对
    `/* … */` 维护跨行状态，整个块内一律不参与识别。
    """
    lines = sf.text.splitlines() if hasattr(sf, "text") else []
    out: list[str] = []
    for start, length in ranges:
        begin = max(int(start) - 1, 0)
        end = begin + max(int(length), 1)
        # hunk 起点可能**正好落在块注释内部**（Codeup 的 hunk 起止行与本地 git 略有差异），
        # 此时首行没有 `/*` 前缀，只看前缀会把它当代码。向上回看定位是否在块注释里。
        in_block = _starts_inside_block(lines, begin)
        for line in lines[begin:end]:
            text = line.strip()
            if not text:
                continue
            if in_block:
                if "*/" in text:
                    in_block = False
                continue
            if text.startswith(("/*", "{/*")):
                in_block = "*/" not in text
                continue
            if is_comment_line(text):
                continue
            out.append(text)
            if len(out) >= max_lines:
                return out
    return out


def hunk_text(sf: Any, ranges: list[tuple[int, int]], *, max_lines: int = 400) -> str:
    """把 hunk 区间内的有效代码行拼成待识别文本（换行分隔，供 `detect` 消费）。"""
    return "\n".join(changed_lines(sf, ranges, max_lines=max_lines))


def _rules_for(tag: str, kind: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """某语义在指定语言族下的 (强信号, 弱信号)；未知语言族取两边并集（宁全勿漏）。"""
    extra = _KIND_EXTRA.get(kind, {})
    if not extra:  # 未知语言族：两类补充信号都算，避免漏检
        extra = dict(_KIND_EXTRA["frontend"])
        for name, kws in _KIND_EXTRA["backend"].items():
            extra[name] = tuple(dict.fromkeys((*extra.get(name, ()), *kws)))
    strong = tuple(dict.fromkeys((*_STRONG.get(tag, ()), *extra.get(tag, ()))))
    return strong, _WEAK.get(tag, ())


def detect(text: str, file_kind: str = "") -> list[SemanticTag]:
    """识别文本里的变更语义，按 `_TAG_ORDER` 返回（命中 ≥2 个关键词且含强信号）。"""
    lowered = (text or "").lower()
    if not lowered.strip():
        return []
    tags: list[SemanticTag] = []
    for name in _TAG_ORDER:
        strong, weak = _rules_for(name, file_kind)
        strong_hits = [kw for kw in strong if kw in lowered]
        if not strong_hits:
            continue
        hits = list(dict.fromkeys([*strong_hits, *[kw for kw in weak if kw in lowered]]))
        if len(hits) < 2:
            continue
        tags.append(SemanticTag(name=name, keywords=hits, strong=strong_hits))
    return tags


def dimensions_of(tags: list[SemanticTag], *, max_dims: int = 3) -> list[tuple[str, str]]:
    """语义 → ``[(行为维度, 子维度)]``，按标签优先级取前 `max_dims` 个（去重保序）。

    只做「语义优先级」排序，不与类型维度合并——合并（含 scope 过滤、核心维度保留、
    总上限截断）由 `tp_expand._merge_semantic` 负责，避免两处各写一套预算逻辑。
    """
    out: list[tuple[str, str]] = []
    for tag in tags:
        for pair in _TAG_DIMS.get(tag.name, ()):
            if pair in out:
                continue
            out.append(pair)
            if len(out) >= max(1, max_dims):
                return out
    return out


def tag_names(tags: list[SemanticTag]) -> list[str]:
    """语义名列表（报告/统计用）。"""
    return [t.name for t in tags]
