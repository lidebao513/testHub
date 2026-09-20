"""引擎 · 安全期望值来源（F8）：从代码的**真实鉴权接线**推导「这个接口该断言什么」。

为什么必须做
------------
早期实现把安全用例的期望**写死**成「无凭证/越权访问被拒绝（401/403）」，于是：
- 本来就该公开的接口（`/health`、`/login`、静态资源）被判成「鉴权缺失 → 未通过」，
  产出**假失败**——把设计意图当缺陷报；
- 确实需要鉴权的接口，期望值与代码无关，等于**没有验证「接线是否正确」**，
  只是把模板抄了一遍。

本模块只做一件事：扫源码找出「鉴权接线」的证据，据此把每个接口归入三种模式之一
（`core.enums.AuthMode`）：

| 模式 | 判据 | 安全用例的期望 |
|---|---|---|
| `REQUIRED` | 检测到强制鉴权接线（路由级依赖 / 装饰器 / 应用级依赖） | 无凭证应被拒（401/403） |
| `OPTIONAL` | 有接线，但存在「未配置即放行」的占位分支 | **仍应被拒**；未通过时点名「这是配置/占位实现问题」 |
| `ABSENT` | 未检测到任何鉴权接线 | 按公开接口断言：可访问且不泄露敏感字段（附人工确认项） |

`OPTIONAL` 为什么仍按「应被拒」断言：占位实现意味着**默认部署就是未鉴权暴露**——
把它判成「通过」会把一个真实的安全问题洗掉。宁可保留这条发现，只把**归因**写清楚。

诚实边界（务必知道）
--------------------
1. 本模块是**启发式文本/AST 信号扫描**，不是数据流分析：它只能回答「代码里有没有出现
   鉴权接线」，不能证明「该接线在所有路径上都生效」。
2. 每个结论都带 `evidence`（`文件:行号`），供人工复核；无证据的结论不会产生。
3. 运行期配置（例如令牌是否真的配了）不在代码里，故 `OPTIONAL` 同时标注为
   「配置相关」——这正是「同一个接口在 A 环境被拒、B 环境放行」的成因。

依赖方向严格向下（只 import core）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.enums import AuthMode
from engine.scan import SourceFile


# ---- 全局接线：应用级依赖 / 中间件（命中即「所有接口都需要鉴权」）----
_GLOBAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"add_middleware\s*\(", re.IGNORECASE),
    re.compile(r"(?:app|router|api)\s*=\s*(?:FastAPI|APIRouter)\s*\([^)]*dependencies\s*=", re.S),
    re.compile(r"include_router\s*\([^)]*dependencies\s*=", re.S),
)

# ---- 路由级接线：依赖注入 / 装饰器 / 安全方案 ----
_ROUTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Depends\s*\(\s*(?:require_|verify_|check_)?[Aa]uth\w*\s*\)"),
    re.compile(r"Depends\s*\(\s*(?:get_)?current_user\w*\s*\)"),
    re.compile(r"Security\s*\("),
    re.compile(
        r"@\w*(?:login_required|requires?_auth|authenticated|permission_required|jwt_required)\b"
    ),
    re.compile(r"HTTPBearer\s*\("),
    re.compile(r"OAuth2PasswordBearer\s*\("),
)

# ---- 令牌校验信号：出现即说明「有人在校验凭证」----
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Authorization", re.IGNORECASE),
    re.compile(r"\bBearer\b"),
    re.compile(r"X-Auth-Token", re.IGNORECASE),
    re.compile(r"verify_token|decode_token|validate_token|check_token"),
)

# ---- 占位实现：`if not <某鉴权配置>: return`（未配置即放行）----
# 这是最关键的一条：它把「有接线」细化为「接线可被配置关闭」，直接决定
# 「这条失败到底是代码缺陷还是环境未配置」的归因。
_PASSTHROUGH_RE = re.compile(
    r"if\s+not\s+[^\n:]*auth[^\n:]*:[^\S\n]*\n\s*return\b",
    re.IGNORECASE,
)

# 每类信号最多留存的证据条数（防止产物膨胀；统计数仍为全量）
_MAX_EVIDENCE_PER_KIND = 3

# 鉴权信号只在 Python / TS 源码里有意义；html 页面里的 "Authorization" 属噪声
_SCAN_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx")


@dataclass
class AuthProfile:
    """一次扫描得到的鉴权接线画像（供测试点展开与执行器判定共用）。"""

    global_auth: bool = False  # 是否存在应用级鉴权接线
    passthrough: bool = False  # 是否存在「未配置即放行」的占位分支
    files_with_auth: set[str] = field(default_factory=set)  # 含路由级/令牌级信号的文件
    evidence: list[str] = field(default_factory=list)  # `文件:行号 片段`，人工可复核
    signals: dict[str, int] = field(default_factory=dict)  # 各类信号命中次数

    @property
    def wired(self) -> bool:
        """是否检测到任何鉴权接线（全局或文件级）。"""
        return bool(self.global_auth or self.files_with_auth)

    @property
    def mode(self) -> str:
        """全仓口径的鉴权模式（单文件判定请用 `mode_for`）。"""
        if not self.wired:
            return AuthMode.ABSENT.value
        return AuthMode.OPTIONAL.value if self.passthrough else AuthMode.REQUIRED.value

    def mode_for(self, file_path: str) -> str:
        """判定某个接口（按来源文件）应采用哪种鉴权期望。

        优先级：占位实现 > 有接线 > 无接线。全局接线命中时所有接口都算「有接线」。
        """
        has_wiring = self.global_auth or _norm(file_path) in self.files_with_auth
        if not has_wiring:
            return AuthMode.ABSENT.value
        return AuthMode.OPTIONAL.value if self.passthrough else AuthMode.REQUIRED.value

    def summary_line(self) -> str:
        """一行可读摘要（写入运行备注，让「期望值从哪来」可见）。"""
        counts = "、".join(f"{k} {v}" for k, v in sorted(self.signals.items()) if v)
        tail = f"（信号：{counts}）" if counts else ""
        return (
            f"鉴权接线扫描（F8）：模式={self.mode}，"
            f"全局接线={'是' if self.global_auth else '否'}，"
            f"占位实现={'是' if self.passthrough else '否'}，"
            f"含接线文件 {len(self.files_with_auth)} 个{tail}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "global_auth": self.global_auth,
            "passthrough": self.passthrough,
            "files_with_auth": sorted(self.files_with_auth),
            "signals": dict(self.signals),
            "evidence": list(self.evidence),
        }


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/")


def _count(patterns: tuple[re.Pattern[str], ...], text: str) -> int:
    return sum(len(p.findall(text)) for p in patterns)


def _evidence_of(sf: SourceFile, patterns: tuple[re.Pattern[str], ...], label: str) -> list[str]:
    """产出可复核的证据行（`文件:行号  片段`），每类最多 `_MAX_EVIDENCE_PER_KIND` 条。"""
    out: list[str] = []
    for lineno, line in enumerate(sf.text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(pat.search(line) for pat in patterns):
            out.append(f"{_norm(sf.rel)}:{lineno} [{label}] {stripped[:80]}")
            if len(out) >= _MAX_EVIDENCE_PER_KIND:
                break
    return out


def scan_auth(files: dict[str, SourceFile]) -> AuthProfile:
    """扫描源码，得到鉴权接线画像。

    `files` 为 `engine.scan.Scanner.index()` 的产物（`{相对路径: SourceFile}`）。
    只扫 Python / 前端源码；html 模板中的 `Authorization` 字样不参与判定（噪声）。
    """
    profile = AuthProfile()
    signals: dict[str, int] = {}
    for sf in files.values():
        if not sf.rel.lower().endswith(_SCAN_EXTS):
            continue
        text = sf.text
        if not text:
            continue
        rel = _norm(sf.rel)

        global_hits = _count(_GLOBAL_PATTERNS, text)
        route_hits = _count(_ROUTE_PATTERNS, text)
        token_hits = _count(_TOKEN_PATTERNS, text)
        if global_hits:
            signals["全局接线"] = signals.get("全局接线", 0) + global_hits
            profile.global_auth = True
            profile.evidence.extend(_evidence_of(sf, _GLOBAL_PATTERNS, "全局接线"))
        if route_hits:
            signals["路由依赖/装饰器"] = signals.get("路由依赖/装饰器", 0) + route_hits
            profile.files_with_auth.add(rel)
            profile.evidence.extend(_evidence_of(sf, _ROUTE_PATTERNS, "路由接线"))
        if token_hits:
            signals["令牌校验"] = signals.get("令牌校验", 0) + token_hits
            profile.files_with_auth.add(rel)
        if _PASSTHROUGH_RE.search(text):
            profile.passthrough = True
            signals["未配置即放行"] = signals.get("未配置即放行", 0) + 1
            profile.evidence.extend(_evidence_of(sf, (_PASSTHROUGH_RE,), "占位实现"))

    profile.signals = signals
    return profile


def mode_of_fp(fp: Any, profile: AuthProfile | None) -> str:
    """功能点 → 鉴权模式（`profile` 为空时退回 `REQUIRED`，保持 v1.0 行为）。"""
    if profile is None:
        return AuthMode.REQUIRED.value
    return profile.mode_for(str(getattr(fp, "file_path", "") or ""))
