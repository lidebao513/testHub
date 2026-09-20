"""统一智能输入框：把一段自由文本解析成结构化运行参数（A1 · 流程二入口）。

为什么需要
----------
用户手里的信息天然是**混排**的：一条消息里同时给出「测试地址 / 账号 / 密码 /
动态口令 / 代码路径」。而 CLI 参数与 HTTP 字段是**结构化**的。要求用户先自己想清楚
「哪个值填哪个框」，既违背真实习惯，也容易漏填——漏填的后果不是报错，而是
**静默跑成匿名访问**（用例照样生成，UI 层却全是登录页）。本模块负责把混排文本
切回结构化字段，作为「统一智能输入框」的内核。

设计原则
--------
- **纯规则、无网络、无 LLM**：可单测、可复现，不产生额外调用成本；
- **标签优先、形态兜底**：`账号: xxx` / `账号 xxx` 这类显式标签最可靠；无标签的裸 token 按形态
  （URL / 邮箱 / 已存在目录 / 纯数字口令 / 密码特征）推断；
- **不臆造**：识别不出的片段原样收进 `unknown` 并如实回报，**绝不静默丢弃**；
- **凭证红线**：结果含明文凭证，`redacted()` 是唯一允许打印 / 返回 / 落库的视图
  （见 `P3_UI生成_详细设计.md` §4.2）。调用方**不得**直接把 dataclass 塞进日志或产物。

字段别名集中在 `_LABEL_GROUPS`（唯一真值），新增写法只改这一处。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.enums import MODE_CHOICES, MODE_FULL, MODE_INCREMENTAL


__all__ = ["AutoInputResult", "mask_secret", "parse_auto_input"]


# ============================================================================
# 字段与别名（唯一真值）
# ============================================================================
_LABEL_GROUPS: dict[str, tuple[str, ...]] = {
    "url": (
        "url",
        "地址",
        "测试地址",
        "被测地址",
        "环境地址",
        "网址",
        "链接",
        "link",
        "base_url",
    ),
    "login_url": ("登录地址", "登录url", "登录页", "登录页面", "login_url", "loginurl"),
    "user": (
        "账号",
        "账户",
        "用户名",
        "用户",
        "登录账号",
        "登录用户名",
        "account",
        "user",
        "username",
        "邮箱",
        "email",
    ),
    "password": ("密码", "登录密码", "口令", "password", "passwd", "pwd"),
    "otp": (
        "动态码",
        "动态口令",
        "动态密码",
        "验证码",
        "一次性口令",
        "令牌码",
        "otp",
        "code",
        "mfa",
    ),
    "local_path": (
        "路径",
        "代码路径",
        "代码目录",
        "目录",
        "仓库路径",
        "仓库",
        "代码",
        "path",
        "local_path",
        "repo",
    ),
    "scopes": ("范围", "测试范围", "维度", "覆盖", "scope", "scopes"),
    "mode": ("模式", "通道", "mode"),
    "base": ("基线", "base", "base_ref"),
    "target": ("目标", "target", "target_ref"),
    "project_name": ("项目名", "项目名称", "项目", "工程名", "name", "project"),
    "routes": ("路由", "页面路径", "route", "routes"),
}


def _normalize_label(raw: str) -> str:
    """归一化标签：去掉空格 / 下划线 / 连字符后转小写（`Base URL` → `baseurl`）。"""
    return re.sub(r"[\s_\-]", "", raw).strip().lower()


# 归一化别名 → 字段名（构造期展开，避免运行时线性查找）
_LABEL_ALIASES: dict[str, str] = {
    _normalize_label(alias): field_name
    for field_name, aliases in _LABEL_GROUPS.items()
    for alias in aliases
}


# ============================================================================
# 结果
# ============================================================================
@dataclass
class AutoInputResult:
    """智能输入框的解析结果（**含明文凭证，不得直接外泄**）。"""

    url: str = ""
    login_url: str = ""
    user: str = ""
    password: str = ""
    otp: str = ""
    local_path: str = ""
    scopes: list[str] = field(default_factory=list)
    mode: str = ""
    base: str = ""
    target: str = ""
    project_name: str = ""
    routes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def has_credentials(self) -> bool:
        """是否凑齐了「账号 + 密码」这一对（决定能否走登录态发现）。"""
        return bool(self.user and self.password)

    def recognized_fields(self) -> list[str]:
        """已识别的字段名清单（不含凭证取值，可安全输出）。"""
        return sorted(
            name
            for name in (
                "url",
                "login_url",
                "user",
                "password",
                "otp",
                "local_path",
                "scopes",
                "mode",
                "base",
                "target",
                "project_name",
                "routes",
            )
            if getattr(self, name)
        )

    def redacted(self) -> dict[str, Any]:
        """可安全输出的视图（**唯一**允许打印 / 返回 / 落库的形态）。"""
        return {
            "url": self.url,
            "login_url": self.login_url,
            "user": mask_secret(self.user),
            "password": "***" if self.password else "",
            "otp": "***" if self.otp else "",
            "local_path": self.local_path,
            "scopes": list(self.scopes),
            "mode": self.mode,
            "base": self.base,
            "target": self.target,
            "project_name": self.project_name,
            "routes": list(self.routes),
            "unknown": [_mask_unknown(entry) for entry in self.unknown],
            "recognized": self.recognized_fields(),
            "has_credentials": self.has_credentials(),
        }


def mask_secret(value: str, *, keep: int = 2) -> str:
    """掩码展示：保留首尾各 `keep` 个字符，中间以 `*` 替代（长度也一并模糊化）。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * 4
    return f"{value[:keep]}****{value[-keep:]}"


def _mask_unknown(entry: str) -> str:
    """未识别片段可能是漏配的凭证 → 输出前一律掩码其值部分。"""
    if "=" in entry:
        key, _, value = entry.partition("=")
        return f"{key}={mask_secret(value)}"
    return mask_secret(entry) if len(entry) > 8 else entry


# ============================================================================
# 形态识别（裸 token 兜底）
# ============================================================================
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_OTP_RE = re.compile(r"^\d{4,8}$")
_LOGIN_HINT_RE = re.compile(r"(login|signin|sign-in|登录)", re.IGNORECASE)
_TOKEN_SPLIT_RE = re.compile(r"[\s,;，；、|]+")
_QUOTES = "\"'“”‘’"


def _is_existing_dir(token: str) -> bool:
    """token 是否指向本机已存在的目录（唯一用到文件系统的一处，只读探测）。"""
    try:
        return Path(token).expanduser().is_dir()
    except (OSError, ValueError):
        return False


def _looks_like_password(token: str) -> bool:
    """密码特征：≥6 位、同时含字母与数字（避免把普通词组误判成密码）。"""
    if len(token) < 6 or _URL_RE.match(token):
        return False
    return bool(re.search(r"[A-Za-z]", token) and re.search(r"\d", token))


def _candidate_field(token: str, password_seen: bool) -> str:
    """按形态给出候选字段（未做「是否已被占用」判断）；无从判断返回空串。

    判定顺序即优先级：URL → 邮箱 → 已存在目录 → 数字口令 → 密码特征。
    为什么「数字口令」要求密码已有着落：单独的 6 位数字既可能是口令也可能是别的编号，
    没有密码作上下文时**宁可如实报未识别**，也不猜。
    """
    if _URL_RE.match(token):
        return "login_url" if _LOGIN_HINT_RE.search(token) else "url"
    if _EMAIL_RE.match(token):
        return "user"
    if _is_existing_dir(token):
        return "local_path"
    if _OTP_RE.match(token) and password_seen:
        return "otp"
    return "password" if _looks_like_password(token) else ""


def _shape_field(target: AutoInputResult, token: str) -> str | None:
    """形态判定：返回该 token 应写入的字段名；字段已占用或无从判断则返回 None。"""
    field = _candidate_field(token, bool(target.password))
    return field if field and not getattr(target, field) else None


def _classify_token(target: AutoInputResult, token: str) -> None:
    """按形态把裸 token 归入某个字段；无法归类则如实记入 `unknown`。"""
    field = _shape_field(target, token)
    if field is None:
        target.unknown.append(token)
        return
    value = str(Path(token).expanduser()) if field == "local_path" else token
    setattr(target, field, value)


# ============================================================================
# 标签值写入（表驱动：字段新增只改 _SETTERS，不引入分支链）
# ============================================================================
def _set_first(attr: str) -> Callable[[AutoInputResult, str], None]:
    """生成「字段为空才写入」的 setter（同一标签重复出现时保留首个，不覆盖）。"""

    def setter(target: AutoInputResult, value: str) -> None:
        if not getattr(target, attr):
            setattr(target, attr, value)

    return setter


def _set_scopes(target: AutoInputResult, value: str) -> None:
    if target.scopes:
        return
    target.scopes = [p for p in re.split(r"[\s,，、+/|和及]+", value) if p]


def _set_user(target: AutoInputResult, value: str) -> None:
    """账号：邮箱结尾的 `.` / `。` 是句子标点（邮箱不可能以点结尾），顺手归一化。"""
    if target.user:
        return
    target.user = value.rstrip(".。")


def _set_routes(target: AutoInputResult, value: str) -> None:
    if target.routes:
        return
    target.routes = [p for p in re.split(r"[\s,，、|]+", value) if p]


def _set_mode(target: AutoInputResult, value: str) -> None:
    """模式归一化：接受「增量 / incremental」「全量 / full」等写法，其余如实报未识别。"""
    if target.mode:
        return
    raw = value.strip().lower()
    if "增量" in value or raw.startswith("incr"):
        target.mode = MODE_INCREMENTAL
    elif "全量" in value or raw.startswith("full"):
        target.mode = MODE_FULL
    else:
        target.unknown.append(f"模式={value}")
        return
    if target.mode not in MODE_CHOICES:  # 防枚举漂移的兜底断言
        target.unknown.append(f"模式={value}")
        target.mode = ""


_SETTERS: dict[str, Callable[[AutoInputResult, str], None]] = {
    "url": _set_first("url"),
    "login_url": _set_first("login_url"),
    "user": _set_user,
    "password": _set_first("password"),
    "otp": _set_first("otp"),
    "local_path": _set_first("local_path"),
    "base": _set_first("base"),
    "target": _set_first("target"),
    "project_name": _set_first("project_name"),
    "scopes": _set_scopes,
    "routes": _set_routes,
    "mode": _set_mode,
}


# ============================================================================
# 主解析
# ============================================================================
# 两种标签写法并存——真实输入里两种都常见：
#   1. `标签: 值`（显式分隔符，值可含空格，一直取到下一个标签）；
#   2. `标签 值`（**空格分隔，无分隔符**）——中文习惯写法，如「账号 test004 密码 xxx」。
#      此式**只认已知别名**，且值只取**紧随其后的单个 token**：否则整句里的普通词都会被
#      当成标签，且「值恰好长得像标签」（如用户名就叫 `user`）时会把整段吞掉。
#
# 两条硬约束（都踩过坑）：
#   1. 标签至少 2 个字符——否则 `C:\code\repo` 里的 `C:` 会被当成标签（Windows 盘符），
#      整条路径被吞进「未知片段」，裸串路径识别直接失效；
#   2. 显式分隔符式的值不得以 `//` 或反斜杠开头——`http://…` 的 `http:` 是 URL 方案名，
#      `D:\…` 的 `D:` 是盘符，都不是标签。
_LABEL_COLON_RE = re.compile(r"(?:^|[\s,;，；、|])([A-Za-z_\u4e00-\u9fff]{2,16})\s*[:=：＝]\s*")
_LABEL_SPACE_RE = re.compile(r"(?:^|[\s,;，；、|])([A-Za-z_\u4e00-\u9fff]{2,16})[ \t]+(\S+)")


def _label_candidates(text: str) -> list[tuple[int, int, int | None, str]]:
    """收集候选标签位点，返回 `(标签起始, 值起点, 值的硬上限|None, 标签)`（已排序去重叠）。

    值的硬上限仅空格式有（= 紧随 token 的结束位置）；显式分隔符式为 `None`，
    表示值可继续延伸，由调用方截断到下一个标签起点。
    """
    raw: list[tuple[int, int, int | None, str, int]] = []
    for match in _LABEL_COLON_RE.finditer(text):
        following = text[match.end() : match.end() + 2]
        if following == "//" or following.startswith("\\"):
            continue  # `http://…` 的 `http:`；`D:\…` 的 `D:`
        raw.append((match.start(), match.end(), None, match.group(1), 0))
    for match in _LABEL_SPACE_RE.finditer(text):
        if _normalize_label(match.group(1)) in _LABEL_ALIASES:
            raw.append((match.start(), match.start(2), match.end(2), match.group(1), 1))
    raw.sort(key=lambda hit: (hit[0], hit[4]))

    kept: list[tuple[int, int, int | None, str]] = []
    for start, value_start, hard_end, label, _priority in raw:
        if kept:
            prev_value_start, prev_hard_end = kept[-1][1], kept[-1][2]
            inside_value = prev_hard_end is not None and start < prev_hard_end
            if start < prev_value_start or inside_value:
                continue  # 落在上一个标签或它已消费的值里 → 是同一段的尾巴，丢弃
        kept.append((start, value_start, hard_end, label))
    return kept


def _label_spans(text: str) -> list[tuple[int, int, str, str]]:
    """切出所有标签片段，返回 `(起始, 结束, 标签, 值)`。"""
    candidates = _label_candidates(text)
    spans: list[tuple[int, int, str, str]] = []
    for idx, (start, value_start, hard_end, label) in enumerate(candidates):
        next_start = candidates[idx + 1][0] if idx + 1 < len(candidates) else len(text)
        value_end = next_start if hard_end is None else min(hard_end, next_start)
        newline = text.find("\n", value_start)
        if newline != -1:
            value_end = min(value_end, newline)
        value = text[value_start:value_end].strip().strip(_QUOTES)
        value = value.strip(",;，；、|").strip()
        if value:
            spans.append((start, value_end, label, value))
    return spans


def _split_tokens(text: str) -> list[str]:
    tokens = []
    for raw in _TOKEN_SPLIT_RE.split(text):
        token = raw.strip().strip(_QUOTES).strip("。.,;，；、")
        if token:
            tokens.append(token)
    return tokens


def _bare_tokens(text: str, spans: list[tuple[int, int, str, str]]) -> list[str]:
    """取出未被任何标签片段覆盖的裸 token（顺序保留）。"""
    if not spans:
        return _split_tokens(text)
    chunks: list[str] = []
    cursor = 0
    for start, end, _label, _value in spans:
        chunks.append(text[cursor:start])
        cursor = end
    chunks.append(text[cursor:])
    tokens: list[str] = []
    for chunk in chunks:
        tokens.extend(_split_tokens(chunk))
    return tokens


def _finalize(result: AutoInputResult) -> None:
    """收尾归一化：

    1. 只给了「地址」但该地址就是登录页 → 拆成 `login_url=原地址` + `url=源地址`。
       为什么必须拆：运行时发现把 `url` 当**应用入口**（用于拼接相对路由），
       若入口是 `/login`，抓到的每个路由都会落在登录页上（静默全错）。
    2. 只给了登录页时，用其源地址补出被测环境根地址。
    """
    if result.url and not result.login_url and _LOGIN_HINT_RE.search(result.url):
        parts = urlparse(result.url)
        if parts.scheme and parts.netloc:
            result.login_url = result.url
            result.url = f"{parts.scheme}://{parts.netloc}"
    if result.login_url and not result.url:
        parts = urlparse(result.login_url)
        if parts.scheme and parts.netloc:
            result.url = f"{parts.scheme}://{parts.netloc}"


def parse_auto_input(text: str) -> AutoInputResult:
    """把一段混排文本解析为结构化运行参数（纯函数，反复调用结果一致）。

    支持三种写法混排：
        标签式（分隔符）： `地址: http://host:8090/chat 账号: t@ft 密码: Tp0909@test 动态码: 260909`
        标签式（空格）：   `地址 http://host:8090/chat 账号 test004 密码 Tp0909@test 范围 正常,异常`
        裸串式：           `http://host:8090/chat t@ft Tp0909@test 260909`
    未被识别的部分进 `unknown`，由调用方一并回报给用户。
    """
    result = AutoInputResult()
    if not text or not text.strip():
        return result
    spans = _label_spans(text)
    for _start, _end, label, value in spans:
        field_name = _LABEL_ALIASES.get(_normalize_label(label))
        if field_name is None:
            result.unknown.append(f"{label}={value}")
            continue
        _SETTERS[field_name](result, value)
    for token in _bare_tokens(text, spans):
        _classify_token(result, token)
    _finalize(result)
    return result
